"""Evidence export preserves scoped raw records and never upgrades missing measurements."""

import json
import sqlite3
from pathlib import Path

import pytest

from owcopilot.content.hash import content_hash
from owcopilot.content.models import ContentBundle
from owcopilot.workflow.evidence import export_task_evidence, source_manifest
from owcopilot.workflow.models import NodeOutcome, TaskInput, TaskRecord, now
from owcopilot.workflow.store import WorkflowStore


def test_source_manifest_rejects_same_root_alias_to_secret_without_reading_it(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    secret = workspace / ".env"
    secret.write_text("PRIVATE=value", encoding="utf-8")
    declared = workspace / "pyproject.toml"
    original_resolve = Path.resolve
    original_read = Path.read_bytes

    def resolve(path, *args, **kwargs):
        return (
            original_resolve(secret)
            if path == declared
            else original_resolve(path, *args, **kwargs)
        )

    def read(path):
        assert path != secret, "Secret contents must never be read, even just for a hash"
        return original_read(path)

    monkeypatch.setattr(Path, "resolve", resolve)
    monkeypatch.setattr(Path, "read_bytes", read)
    manifest = source_manifest(workspace)
    assert "pyproject.toml" in manifest["excluded_outside_workspace"]
    assert manifest["files"] == []


def create_task(store, project):
    bundle = ContentBundle()
    task = TaskRecord(
        project=project,
        request=TaskInput(brief=f"Explicit development trace for {project}"),
        base_version="v1",
        base_hash=content_hash(bundle),
        base_bundle=bundle.model_dump(mode="json"),
    )
    store.save_task(task)
    run, _ = store.create_run(task)
    return task, run


def test_export_rejects_cross_project_and_excludes_unrelated_runs_and_workflows(tmp_path):
    store = WorkflowStore(tmp_path / "workflow.sqlite3")
    task, run = create_task(store, "a")
    other, other_run = create_task(store, "b")
    own_workflow = store.save_workflow(
        {"project": "a", "source_task_id": task.id, "source_run_id": run.id, "name": "own"}
    )
    store.save_workflow(
        {
            "project": "b",
            "source_task_id": other.id,
            "source_run_id": other_run.id,
            "name": "foreign",
        }
    )
    store.save_workflow({"project": "a", "source_task_id": "unrelated", "name": "not this task"})
    with pytest.raises(KeyError, match="当前项目"):
        export_task_evidence(store, "b", task.id)
    exported = export_task_evidence(store, "a", task.id)
    assert exported["scope"]["kind"] == "development_trace"
    assert [row["id"] for row in exported["runs"]] == [run.id]
    assert {row["run_id"] for row in exported["events"]} == {run.id}
    assert [row["id"] for row in exported["workflows"]] == [own_workflow["id"]]
    assert other_run.id not in json.dumps(exported)
    assert exported["manifest"]["historical_execution_code_version"] == "unknown"


def test_export_includes_over_500_events_failures_budgets_and_legacy_unknown(tmp_path):
    store = WorkflowStore(tmp_path / "workflow.sqlite3")
    task, run = create_task(store, "a")
    budget = f"{task.id}:1:draft_quest"
    attempt = store.claim_attempt(budget, run.id, "writer", operation="draft_quest")
    call_id = store.begin_model_call(
        run.id, budget, "writer", attempt, {"task": "explicit fixture"}
    )
    store.finish_model_call(call_id, error="Explicit fixture failure retained")
    store.complete_node(
        run.id,
        NodeOutcome(
            node_id="writer",
            operation="draft_quest",
            status="skipped_nonretryable",
            attempts=attempt,
            budget_id=budget,
            error="Explicit fixture failure retained",
        ),
    )
    with store.connection() as db:
        db.executemany(
            "INSERT INTO events(run_id,event_type,payload,created_at) VALUES(?,?,?,?)",
            [(run.id, "fixture", json.dumps({"index": index}), now()) for index in range(501)],
        )
        raw_call = db.execute("SELECT payload FROM model_calls WHERE id=?", (call_id,)).fetchone()[
            0
        ]
    exported = export_task_evidence(store, "a", task.id)
    fixture_events = [row for row in exported["events"] if row["event_type"] == "fixture"]
    assert len(fixture_events) == 501
    assert fixture_events[-1]["record"] == {"index": 500}
    call = exported["model_calls"][0]
    assert call["payload_json"] == raw_call
    assert call["record"]["status"] == "failed"
    assert "measurement" not in call["record"]
    assert call["measurement"]["status"] == "legacy_missing"
    assert call["measurement"]["input_tokens"] is None
    assert call["measurement"]["cost_usd"] is None
    assert call["measurement"]["cost_status"] == "unknown"
    assert exported["budgets"] == [{"id": budget, "attempts": 1}]
    assert exported["outcomes"][0]["record"]["error"] == "Explicit fixture failure retained"
    assert exported["scope"]["independent_sample_count"] is None


def test_export_does_not_reinitialize_or_change_database_and_manifest_is_allowlisted(
    tmp_path, monkeypatch
):
    store = WorkflowStore(tmp_path / "workflow.sqlite3")
    task, _ = create_task(store, "a")
    with sqlite3.connect(store.path) as db:
        before = list(db.iterdump())
    before_bytes = store.path.read_bytes()

    def forbidden_constructor(*args, **kwargs):
        raise AssertionError("Export must not create or migrate a WorkflowStore")

    monkeypatch.setattr(WorkflowStore, "__init__", forbidden_constructor)
    first = export_task_evidence(store, "a", task.id)
    second = export_task_evidence(store, "a", task.id)
    assert first["manifest"] == second["manifest"]
    with sqlite3.connect(store.path) as db:
        assert list(db.iterdump()) == before
    assert store.path.read_bytes() == before_bytes

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text('[project]\nname = "explicit-fixture"\n')
    (workspace / ".env").write_text("SHOULD_NEVER_BE_READ=private-fixture")
    (workspace / ".git").mkdir()
    (workspace / ".git" / "config").write_text("private-fixture")
    manifest = source_manifest(workspace)
    assert [row["path"] for row in manifest["files"]] == ["pyproject.toml"]
    assert "private-fixture" not in json.dumps(manifest)
    assert manifest["scope"] == "explicit_source_allowlist_at_export_time"
    assert manifest["historical_execution_code_version"] == "unknown"
