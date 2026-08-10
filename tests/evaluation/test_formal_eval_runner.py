"""End-to-end and adversarial tests for the unified formal harness runner."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

import owcopilot.evaluation.formal_runner as formal_runner
from owcopilot.evaluation.formal_artifacts import verify_formal_eval_runs
from owcopilot.evaluation.formal_cohort import (
    FormalCohortCanonicalError,
    FormalCohortError,
    compute_formal_governance_sha256,
    parse_formal_cohort_bytes,
)
from owcopilot.evaluation.formal_runner import (
    FORMAL_SUITE_IDS,
    HARNESS_METRIC_NAME,
    FormalRunnerCollisionError,
    FormalRunnerInputError,
    FormalRunnerPathError,
    FormalRunnerWriteError,
    run_formal_evals,
)
from owcopilot.evaluation.protocol import (
    EvalProvider,
    EvalSplit,
    EvalStatus,
    FormalEvalReport,
    FormalEvalTrace,
    MetricScope,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FORMAL_ROOT = _REPO_ROOT / "benchmarks" / "formal"


def _formal_cli_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("TRANSFORMERS_NO_ADVISORY_WARNINGS", None)
    return env


_SCRIPT = _REPO_ROOT / "scripts" / "run_formal_evals.py"
_RUN_ID = "runner-test-v1"
_GENERATED_AT = "2026-08-03T01:02:03Z"
_SUBJECTIVE_BLOCKED = {
    "game_planner_vertical_e2e",
    "agent_trust_gold",
    "semantic_shadow_eval",
}


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_one_call_runs_all_four_frozen_suites_and_verifies_artifacts(tmp_path: Path) -> None:
    output = tmp_path / "runs"
    result = run_formal_evals(
        output,
        formal_root=_FORMAL_ROOT,
        run_id=_RUN_ID,
        generated_at=_GENERATED_AT,
    )

    assert result.ok
    assert result.exit_code == 0
    assert result.verification.ok
    assert tuple(suite.suite_id for suite in result.suites) == FORMAL_SUITE_IDS
    assert {path.name for path in output.iterdir()} == {*FORMAL_SUITE_IDS, "cohort.json"}
    cohort = parse_formal_cohort_bytes((output / "cohort.json").read_bytes())
    assert cohort.base_run_id == _RUN_ID
    assert cohort.governance_sha256 == compute_formal_governance_sha256(_FORMAL_ROOT)
    assert tuple(entry.suite_id for entry in cohort.suites) == FORMAL_SUITE_IDS
    cohort_by_suite = {entry.suite_id: entry for entry in cohort.suites}

    run_dirs: list[Path] = []
    for suite in result.suites:
        run_dir = output / suite.suite_id
        run_dirs.append(run_dir)
        report = FormalEvalReport.model_validate_json((run_dir / "report.json").read_bytes())
        trace_paths = sorted((run_dir / "traces").glob("*.json"))
        traces = [FormalEvalTrace.model_validate_json(path.read_bytes()) for path in trace_paths]
        assert report.run_id == f"{_RUN_ID}.{suite.suite_id}"
        assert report.provider is EvalProvider.FIXTURE
        assert report.split is EvalSplit.CANARY
        assert report.harness_status is EvalStatus.PASS
        expected_quality = (
            EvalStatus.BLOCKED_EXTERNAL_DATA
            if suite.suite_id in _SUBJECTIVE_BLOCKED
            else EvalStatus.NOT_RUN
        )
        assert report.evaluation_status is expected_quality
        assert report.counts.model_dump() == {
            "total_cases": 2,
            "executed_cases": 2,
            "passed_cases": 1,
            "failed_cases": 1,
            "errored_cases": 0,
            "skipped_cases": 0,
        }
        assert report.primary_metric == HARNESS_METRIC_NAME
        assert len(report.metrics) == 1
        metric = report.metrics[0]
        assert metric.name == HARNESS_METRIC_NAME
        assert metric.scope is MetricScope.HARNESS
        assert metric.sample_size == 2
        assert metric.success_count == 2
        assert metric.value == 1.0
        assert all(item.scope is MetricScope.HARNESS for item in report.metrics)
        assert len(trace_paths) == 2
        assert {trace.status for trace in traces} == {EvalStatus.PASS, EvalStatus.FAIL}
        assert {trace.provider for trace in traces} == {EvalProvider.FIXTURE}
        assert {trace.split for trace in traces} == {EvalSplit.CANARY}
        assert tuple(_sha256(path.read_bytes()) for path in trace_paths) == report.trace_sha256s
        cohort_entry = cohort_by_suite[suite.suite_id]
        assert cohort_entry.run_id == report.run_id
        assert cohort_entry.report_sha256 == _sha256((run_dir / "report.json").read_bytes())
        assert cohort_entry.trace_sha256s == report.trace_sha256s

        for trace in traces:
            assert trace.model is None
            assert trace.usage is None
            assert trace.payload["data_partition"] == "harness_control"
            assert trace.payload["production_path_exercised"] is False
            assert trace.payload["quality_effects_emitted"] is False
            assert trace.payload["real_model_used"] is False
            references = trace.payload["input_artifacts"]
            assert isinstance(references, dict)
            for reference in references.values():
                assert isinstance(reference, dict)
                source = _FORMAL_ROOT / str(reference["path"])
                content = source.read_bytes()
                assert reference["byte_count"] == len(content)
                assert reference["sha256"] == _sha256(content)

    aggregate = verify_formal_eval_runs(run_dirs, formal_root=_FORMAL_ROOT)
    assert aggregate.ok
    assert aggregate.real_model_usage.model_calls == 0
    assert aggregate.real_model_usage.tokens == 0
    assert aggregate.real_model_usage.cost_usd == 0.0


def test_repeated_run_with_explicit_time_and_id_is_byte_deterministic(tmp_path: Path) -> None:
    output = tmp_path / "repeat"
    first = run_formal_evals(
        output,
        formal_root=_FORMAL_ROOT,
        run_id=_RUN_ID,
        generated_at=_GENERATED_AT,
    )
    before = _snapshot(output)
    second = run_formal_evals(
        output,
        formal_root=_FORMAL_ROOT,
        run_id=_RUN_ID,
        generated_at=_GENERATED_AT,
    )
    after = _snapshot(output)
    assert first.ok and second.ok
    assert before == after
    assert len(before) == 13  # four reports, two traces per suite, and cohort.json


def test_governance_digest_is_relocatable_and_cohort_requires_canonical_bytes(
    tmp_path: Path,
) -> None:
    first = tmp_path / "formal-a"
    second = tmp_path / "formal-b"
    shutil.copytree(_FORMAL_ROOT, first)
    shutil.copytree(_FORMAL_ROOT, second)
    assert compute_formal_governance_sha256(first) == compute_formal_governance_sha256(second)

    output = tmp_path / "canonical"
    run_formal_evals(
        output,
        formal_root=_FORMAL_ROOT,
        run_id=_RUN_ID,
        generated_at=_GENERATED_AT,
    )
    content = (output / "cohort.json").read_bytes()
    parse_formal_cohort_bytes(content)
    with pytest.raises(FormalCohortCanonicalError, match="not canonical"):
        parse_formal_cohort_bytes(content + b" ")
    payload = json.loads(content)
    payload["suites"].pop()
    incomplete = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    )
    with pytest.raises(FormalCohortError, match="at least 4 items"):
        parse_formal_cohort_bytes(incomplete)


def test_cohort_completion_is_the_last_atomic_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "last-completion"
    real_write = formal_runner.atomic_write_text
    writes: list[Path] = []

    def record_write(path: str | Path, text: str, **kwargs: object) -> None:
        writes.append(Path(path))
        real_write(path, text, **kwargs)

    monkeypatch.setattr(formal_runner, "atomic_write_text", record_write)
    result = run_formal_evals(
        output,
        formal_root=_FORMAL_ROOT,
        run_id=_RUN_ID,
        generated_at=_GENERATED_AT,
    )
    assert result.ok
    assert len(writes) == 13
    assert writes[-1] == output / "cohort.json"
    assert all(path.name != "cohort.json" for path in writes[:-1])


def test_interrupted_fresh_run_has_no_cohort_and_same_args_resume_missing_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "resume"
    real_write = formal_runner.atomic_write_text
    write_count = 0

    def fail_after_one_complete_suite(path: str | Path, text: str, **kwargs: object) -> None:
        nonlocal write_count
        write_count += 1
        if write_count == 4:
            raise OSError("injected interruption between suites")
        real_write(path, text, **kwargs)

    monkeypatch.setattr(formal_runner, "atomic_write_text", fail_after_one_complete_suite)
    with pytest.raises(FormalRunnerWriteError, match="between suites"):
        run_formal_evals(
            output,
            formal_root=_FORMAL_ROOT,
            run_id=_RUN_ID,
            generated_at=_GENERATED_AT,
        )
    partial = _snapshot(output)
    assert partial
    assert "cohort.json" not in partial

    monkeypatch.setattr(formal_runner, "atomic_write_text", real_write)
    resumed = run_formal_evals(
        output,
        formal_root=_FORMAL_ROOT,
        run_id=_RUN_ID,
        generated_at=_GENERATED_AT,
    )
    assert resumed.ok
    assert len(_snapshot(output)) == 13
    parse_formal_cohort_bytes((output / "cohort.json").read_bytes())


def test_completion_write_failure_leaves_complete_suites_without_false_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "completion-failure"
    real_write = formal_runner.atomic_write_text

    def fail_completion(path: str | Path, text: str, **kwargs: object) -> None:
        if Path(path).name == "cohort.json":
            raise OSError("injected completion failure")
        real_write(path, text, **kwargs)

    monkeypatch.setattr(formal_runner, "atomic_write_text", fail_completion)
    with pytest.raises(FormalRunnerWriteError, match="completion failure"):
        run_formal_evals(
            output,
            formal_root=_FORMAL_ROOT,
            run_id=_RUN_ID,
            generated_at=_GENERATED_AT,
        )
    assert "cohort.json" not in _snapshot(output)
    assert len(_snapshot(output)) == 12

    resumed_writes: list[Path] = []

    def record_resume(path: str | Path, text: str, **kwargs: object) -> None:
        resumed_writes.append(Path(path))
        real_write(path, text, **kwargs)

    monkeypatch.setattr(formal_runner, "atomic_write_text", record_resume)
    resumed = run_formal_evals(
        output,
        formal_root=_FORMAL_ROOT,
        run_id=_RUN_ID,
        generated_at=_GENERATED_AT,
    )
    assert resumed.ok
    assert resumed_writes == [output / "cohort.json"]


def test_different_cohort_collision_is_rejected_before_any_atomic_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "immutable-cohort"
    run_formal_evals(
        output,
        formal_root=_FORMAL_ROOT,
        run_id="cohort-old",
        generated_at="2026-08-03T00:00:00Z",
    )
    before = _snapshot(output)
    writes: list[Path] = []

    def record_unexpected_write(path: str | Path, _text: str, **_kwargs: object) -> None:
        writes.append(Path(path))
        raise AssertionError("preflight must reject before atomic_write_text")

    monkeypatch.setattr(formal_runner, "atomic_write_text", record_unexpected_write)
    with pytest.raises(FormalRunnerCollisionError, match="bytes differ"):
        run_formal_evals(
            output,
            formal_root=_FORMAL_ROOT,
            run_id="cohort-new",
            generated_at="2026-08-03T01:00:00Z",
        )
    assert writes == []
    assert _snapshot(output) == before


def test_concurrent_different_cohorts_cannot_both_publish_green(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "concurrent-cohort"
    entered_preflight = threading.Event()
    release_preflight = threading.Event()
    real_preflight = formal_runner._preflight_output
    first_call_lock = threading.Lock()
    first_call = True

    def hold_first_preflight(*args: object, **kwargs: object) -> object:
        nonlocal first_call
        with first_call_lock:
            should_hold = first_call
            first_call = False
        if should_hold:
            entered_preflight.set()
            assert release_preflight.wait(timeout=30)
        return real_preflight(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(formal_runner, "_preflight_output", hold_first_preflight)
    with ThreadPoolExecutor(max_workers=2) as executor:
        winner_future = executor.submit(
            run_formal_evals,
            output,
            formal_root=_FORMAL_ROOT,
            run_id="concurrent-a",
            generated_at=_GENERATED_AT,
        )
        assert entered_preflight.wait(timeout=30)
        loser_future = executor.submit(
            run_formal_evals,
            output,
            formal_root=_FORMAL_ROOT,
            run_id="concurrent-b",
            generated_at="2026-08-03T02:03:04Z",
        )
        try:
            with pytest.raises(FormalRunnerCollisionError, match="already locked"):
                loser_future.result(timeout=30)
        finally:
            release_preflight.set()
        winner = winner_future.result(timeout=30)

    assert winner.ok
    cohort = parse_formal_cohort_bytes((output / "cohort.json").read_bytes())
    assert cohort.base_run_id == "concurrent-a"
    assert {suite.run_id for suite in winner.suites} == {
        f"concurrent-a.{suite_id}" for suite_id in FORMAL_SUITE_IDS
    }
    assert (output.parent / f".{output.name}.formal-eval.lock").is_file()


def test_postflight_binds_result_to_every_expected_publication_byte(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "postflight-binding"
    real_verify = formal_runner._verify_runs_adapter

    def verify_then_tamper(run_dirs: tuple[Path, ...], formal_root: Path) -> object:
        verification = real_verify(run_dirs, formal_root)
        report_path = run_dirs[0] / "report.json"
        report_path.write_bytes(report_path.read_bytes() + b" ")
        return verification

    monkeypatch.setattr(formal_runner, "_verify_runs_adapter", verify_then_tamper)

    with pytest.raises(FormalRunnerCollisionError, match="published bytes changed"):
        run_formal_evals(
            output,
            formal_root=_FORMAL_ROOT,
            run_id=_RUN_ID,
            generated_at=_GENERATED_AT,
        )


def test_persistent_advisory_lock_file_does_not_block_crash_resume(tmp_path: Path) -> None:
    output = tmp_path / "persistent-lock"
    output.mkdir()
    lock_path = output.parent / f".{output.name}.formal-eval.lock"
    lock_path.write_bytes(b"\0")

    result = run_formal_evals(
        output,
        formal_root=_FORMAL_ROOT,
        run_id=_RUN_ID,
        generated_at=_GENERATED_AT,
    )

    assert result.ok
    assert lock_path.is_file()


def test_windows_publication_lock_adapter_uses_acquire_and_release_modes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, int, int]] = []
    seeks: list[tuple[int, int, int]] = []
    imported: list[str] = []

    class FakeWindowsLocking:
        LK_NBLCK = 303
        LK_UNLCK = 404

        @staticmethod
        def locking(descriptor: int, mode: int, byte_count: int) -> None:
            calls.append((descriptor, mode, byte_count))

    def fake_import(name: str) -> FakeWindowsLocking:
        imported.append(name)
        return FakeWindowsLocking()

    monkeypatch.setattr(formal_runner, "import_module", fake_import)
    monkeypatch.setattr(
        formal_runner.os,
        "lseek",
        lambda descriptor, offset, origin: seeks.append((descriptor, offset, origin)),
    )

    formal_runner._try_windows_publication_lock(42)
    formal_runner._release_windows_publication_lock(42)

    assert imported == ["msvcrt", "msvcrt"]
    assert seeks == [(42, 0, os.SEEK_SET), (42, 0, os.SEEK_SET)]
    assert calls == [(42, 303, 1), (42, 404, 1)]


def test_cross_process_lock_releases_and_orphan_recovers_after_hard_kill(
    tmp_path: Path,
) -> None:
    output = tmp_path / "hard-kill"
    child_code = """
import sys
import time
from pathlib import Path
from owcopilot.evaluation.formal_runner import _publication_root_lock

root = Path(sys.argv[1])
traces = root / "agent_trust_gold" / "traces"
traces.mkdir(parents=True)
(traces / ".001.json.deadbeef.tmp").write_bytes(b"partial")
with _publication_root_lock(root.resolve()):
    print("LOCKED", flush=True)
    while True:
        time.sleep(1)
"""
    child = subprocess.Popen(
        [sys.executable, "-c", child_code, str(output)],
        cwd=Path.cwd(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "LOCKED"
        with pytest.raises(FormalRunnerCollisionError, match="another process"):
            run_formal_evals(
                output,
                formal_root=_FORMAL_ROOT,
                run_id=_RUN_ID,
                generated_at=_GENERATED_AT,
            )
    finally:
        child.kill()
        child.wait(timeout=30)

    resumed = run_formal_evals(
        output,
        formal_root=_FORMAL_ROOT,
        run_id=_RUN_ID,
        generated_at=_GENERATED_AT,
    )
    assert resumed.ok
    assert not (output / "agent_trust_gold" / "traces" / ".001.json.deadbeef.tmp").exists()


def test_verified_atomic_write_orphans_are_removed_and_resume(tmp_path: Path) -> None:
    output = tmp_path / "orphan-resume"
    traces_dir = output / "agent_trust_gold" / "traces"
    traces_dir.mkdir(parents=True)
    trace_orphan = traces_dir / ".001.json.deadbeef.tmp"
    cohort_orphan = output / ".cohort.json.1234abcd.tmp"
    trace_orphan.write_bytes(b'{"hard_kill":')
    cohort_orphan.write_bytes(b"partial")

    result = run_formal_evals(
        output,
        formal_root=_FORMAL_ROOT,
        run_id=_RUN_ID,
        generated_at=_GENERATED_AT,
    )

    assert result.ok
    assert not trace_orphan.exists()
    assert not cohort_orphan.exists()


def test_atomic_orphan_lookalike_is_not_deleted_or_accepted(tmp_path: Path) -> None:
    output = tmp_path / "orphan-lookalike"
    traces_dir = output / "agent_trust_gold" / "traces"
    traces_dir.mkdir(parents=True)
    lookalike = traces_dir / ".001.json.not-nine.tmp"
    lookalike.write_bytes(b"partial")

    with pytest.raises(FormalRunnerPathError, match="unexpected entries"):
        run_formal_evals(
            output,
            formal_root=_FORMAL_ROOT,
            run_id=_RUN_ID,
            generated_at=_GENERATED_AT,
        )

    assert lookalike.read_bytes() == b"partial"


def test_tampered_existing_completion_is_collision_and_no_files_are_rewritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "tampered-completion"
    run_formal_evals(
        output,
        formal_root=_FORMAL_ROOT,
        run_id=_RUN_ID,
        generated_at=_GENERATED_AT,
    )
    cohort_path = output / "cohort.json"
    cohort_path.write_bytes(cohort_path.read_bytes() + b" ")
    before = _snapshot(output)
    writes: list[Path] = []

    def record_unexpected_write(path: str | Path, _text: str, **_kwargs: object) -> None:
        writes.append(Path(path))
        raise AssertionError("preflight must reject before atomic_write_text")

    monkeypatch.setattr(formal_runner, "atomic_write_text", record_unexpected_write)
    with pytest.raises(FormalRunnerCollisionError, match="cohort.json"):
        run_formal_evals(
            output,
            formal_root=_FORMAL_ROOT,
            run_id=_RUN_ID,
            generated_at=_GENERATED_AT,
        )
    assert writes == []
    assert _snapshot(output) == before


def test_control_byte_tamper_fails_before_any_output_is_written(tmp_path: Path) -> None:
    tampered_root = tmp_path / "formal"
    shutil.copytree(_FORMAL_ROOT, tampered_root)
    control = tampered_root / "controls" / "agent_trust_gold" / "cases.json"
    control.write_bytes(control.read_bytes() + b" ")
    output = tmp_path / "must-not-exist"

    with pytest.raises(FormalRunnerInputError, match="frozen control verification failed"):
        run_formal_evals(output, formal_root=tampered_root)
    assert not output.exists()


def test_zero_over_zero_control_is_rejected_as_non_verdict() -> None:
    inputs = formal_runner._load_suite_inputs(_FORMAL_ROOT.resolve(), "failure_isolation_matrix")
    zero_fixture = inputs.fixture.model_copy(update={"cases": ()})
    zero_inputs = replace(inputs, fixture=zero_fixture)

    with pytest.raises(FormalRunnerInputError, match="non-verdict control result NOT_RUN"):
        formal_runner._build_suite_bundle(
            zero_inputs,
            base_run_id=_RUN_ID,
            generated_at=formal_runner._parse_generated_at(_GENERATED_AT),
        )


def test_atomic_write_failure_is_reported_without_truncated_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "write-failure"

    def fail_write(_path: str | Path, _text: str, **_kwargs: object) -> None:
        raise OSError("injected atomic write failure")

    monkeypatch.setattr(formal_runner, "atomic_write_text", fail_write)
    with pytest.raises(FormalRunnerWriteError, match="injected atomic write failure"):
        run_formal_evals(
            output,
            formal_root=_FORMAL_ROOT,
            run_id=_RUN_ID,
            generated_at=_GENERATED_AT,
        )
    assert list(output.rglob("*.json")) == []


def test_output_cannot_overwrite_frozen_governance_or_escape_root(tmp_path: Path) -> None:
    manifest = _FORMAL_ROOT / "suites" / "agent_trust_gold" / "manifest.json"
    before = manifest.read_bytes()
    with pytest.raises(FormalRunnerPathError, match="allowed only below"):
        run_formal_evals(
            _FORMAL_ROOT / "suites",
            formal_root=_FORMAL_ROOT,
            run_id=_RUN_ID,
            generated_at=_GENERATED_AT,
        )
    assert manifest.read_bytes() == before

    root = tmp_path / "contained"
    root.mkdir()
    with pytest.raises(FormalRunnerPathError, match="unsafe output path component"):
        formal_runner._contained_output_path(root, "..", "escaped.json")

    synthetic_formal_root = tmp_path / "synthetic-formal"
    synthetic_formal_root.mkdir()
    nonexistent_protected = synthetic_formal_root / "schemas" / "must-not-be-created"
    with pytest.raises(FormalRunnerPathError, match="allowed only below"):
        formal_runner._prepare_output_root(nonexistent_protected, synthetic_formal_root.resolve())
    assert not nonexistent_protected.exists()


def test_harness_discrimination_regression_returns_exit_code_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_evaluate = formal_runner.evaluate_control_implementation

    def accept_mutant(fixture: object, implementation_id: str) -> object:
        result = real_evaluate(fixture, implementation_id)  # type: ignore[arg-type]
        if implementation_id.endswith(".frozen_mutant.v1"):
            return result.model_copy(update={"harness_status": EvalStatus.PASS})
        return result

    monkeypatch.setattr(formal_runner, "evaluate_control_implementation", accept_mutant)
    result = run_formal_evals(
        tmp_path / "broken-harness",
        formal_root=_FORMAL_ROOT,
        run_id=_RUN_ID,
        generated_at=_GENERATED_AT,
    )
    assert not result.ok
    assert result.exit_code == 1
    assert not result.verification.ok
    assert {
        "RUN_FIXTURE_INDEPENDENT_RESULT_MISMATCH",
        "RUN_FIXTURE_CONTROL_RESULT_MISMATCH",
        "RUN_FIXTURE_REPORT_HARNESS_STATUS_MISMATCH",
    } <= {error.code for error in result.verification.errors}
    assert all(suite.harness_status is EvalStatus.FAIL for suite in result.suites)
    assert all(not suite.harness_ok for suite in result.suites)


def test_cli_runs_all_suites_and_returns_machine_readable_success(tmp_path: Path) -> None:
    output = tmp_path / "cli-runs"
    completed = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--output-dir",
            str(output),
            "--formal-root",
            str(_FORMAL_ROOT),
            "--run-id",
            _RUN_ID,
            "--generated-at",
            _GENERATED_AT,
        ],
        cwd=_REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
        env=_formal_cli_env(),
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["ok"] is True
    assert payload["exit_code"] == 0
    assert [item["suite_id"] for item in payload["suites"]] == list(FORMAL_SUITE_IDS)
    assert payload["verification"]["ok"] is True


def test_cli_different_run_id_collision_returns_exit_two_without_changing_old_bytes(
    tmp_path: Path,
) -> None:
    output = tmp_path / "cli-collision"
    run_formal_evals(
        output,
        formal_root=_FORMAL_ROOT,
        run_id="cli-old",
        generated_at="2026-08-03T00:00:00Z",
    )
    before = _snapshot(output)
    completed = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--output-dir",
            str(output),
            "--formal-root",
            str(_FORMAL_ROOT),
            "--run-id",
            "cli-new",
            "--generated-at",
            "2026-08-03T01:00:00Z",
        ],
        cwd=_REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
        env=_formal_cli_env(),
    )
    assert completed.returncode == 2
    error = json.loads(completed.stderr)
    assert error["ok"] is False
    assert "existing output bytes differ" in error["error"]
    assert _snapshot(output) == before


def test_cli_tamper_returns_nonzero_and_no_run_artifacts(tmp_path: Path) -> None:
    tampered_root = tmp_path / "formal"
    shutil.copytree(_FORMAL_ROOT, tampered_root)
    control = tampered_root / "controls" / "semantic_shadow_eval" / "cases.json"
    control.write_bytes(control.read_bytes() + b" ")
    output = tmp_path / "no-output"
    completed = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--output-dir",
            str(output),
            "--formal-root",
            str(tampered_root),
        ],
        cwd=_REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
        env=_formal_cli_env(),
    )
    assert completed.returncode == 2
    error = json.loads(completed.stderr)
    assert error["ok"] is False
    assert "frozen control verification failed" in error["error"]
    assert not output.exists()
