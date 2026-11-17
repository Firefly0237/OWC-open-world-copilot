"""Read-only development trace export; missing historical measurements stay unknown."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from ..content.hash import content_hash
from .models import now
from .store import WorkflowStore

_SOURCE_FILES = (
    "pyproject.toml",
    "apps/web/package.json",
    "apps/web/package-lock.json",
    "src/owcopilot/workflow/models.py",
    "src/owcopilot/workflow/store.py",
    "src/owcopilot/workflow/planning.py",
    "src/owcopilot/workflow/operators.py",
    "src/owcopilot/workflow/runtime.py",
    "src/owcopilot/workflow/memory.py",
    "src/owcopilot/workflow/bindings.py",
    "src/owcopilot/workflow/catalog.py",
    "src/owcopilot/workflow/learning.py",
    "src/owcopilot/workflow/learning_runtime.py",
    "src/owcopilot/workflow/applicability.py",
    "src/owcopilot/workflow/evidence.py",
    "src/owcopilot/logic/stepper.py",
    "src/owcopilot/logic/temporal.py",
    "src/owcopilot/logic/expr.py",
    "src/owcopilot/logic/semantics.py",
    "src/owcopilot/logic/explore.py",
)


def source_manifest(workspace: str | Path) -> dict[str, Any]:
    """Hash an explicit allowlist now; never infer the code used by historical runs."""
    root = Path(workspace).resolve()
    files = []
    missing = []
    excluded = []
    for relative in _SOURCE_FILES:
        declared = root / relative
        path = declared.resolve()
        if not path.is_relative_to(root) or path != declared:
            excluded.append(relative)
            continue
        if not path.is_file():
            missing.append(relative)
            continue
        data = path.read_bytes()
        files.append(
            {"path": relative, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
        )
    return {
        "schema_version": 1,
        "observed_at": now(),
        "scope": "explicit_source_allowlist_at_export_time",
        "historical_execution_code_version": "unknown",
        "files": files,
        "missing": missing,
        "excluded_outside_workspace": excluded,
        "files_hash": content_hash(files),
        "limits": [
            "Not a historical code snapshot or a complete environment/configuration archive"
        ],
    }


def _rows(db: sqlite3.Connection, query: str, args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in db.execute(query, args).fetchall()]


def _record(row: dict[str, Any]) -> dict[str, Any]:
    return {
        **{key: value for key, value in row.items() if key != "payload"},
        "payload_json": row["payload"],
        "record": json.loads(row["payload"]),
    }


def _measurement(record: dict[str, Any]) -> dict[str, Any]:
    recorded = record.get("measurement")
    if isinstance(recorded, dict):
        return dict(recorded)
    return {
        "status": "legacy_missing",
        "input_tokens": None,
        "output_tokens": None,
        "cached_input_tokens": None,
        "response_model": None,
        "cost_usd": None,
        "cost_status": "unknown",
        "reason": "No persisted measurement; not reconstructed from prompt length or defaults",
    }


def export_task_evidence(store: WorkflowStore, project: str, task_id: str) -> dict[str, Any]:
    """Read one task's complete evidence in one SQLite read transaction without mutation."""
    uri = store.path.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        task_rows = _rows(db, "SELECT * FROM tasks WHERE id=?", (task_id,))
        if not task_rows:
            raise KeyError("任务不存在")
        task = json.loads(task_rows[0]["payload"])
        if task.get("project") != project:
            raise KeyError("任务不属于当前项目")
        runs = _rows(db, "SELECT * FROM runs WHERE task_id=? ORDER BY rowid", (task_id,))
        run_ids = {row["id"] for row in runs}
        outcomes = _rows(
            db,
            "SELECT n.* FROM node_outcomes n JOIN runs r ON r.id=n.run_id "
            "WHERE r.task_id=? ORDER BY n.rowid",
            (task_id,),
        )
        artifacts = [
            row
            for row in _rows(
                db, "SELECT * FROM artifacts WHERE task_id=? ORDER BY rowid", (task_id,)
            )
            if json.loads(row["payload"]).get("task_id") == task_id
            and json.loads(row["payload"]).get("run_id") in run_ids
        ]
        events = _rows(
            db,
            "SELECT e.* FROM events e JOIN runs r ON r.id=e.run_id "
            "WHERE r.task_id=? ORDER BY e.seq",
            (task_id,),
        )
        calls = _rows(
            db,
            "SELECT c.* FROM model_calls c JOIN runs r ON r.id=c.run_id "
            "WHERE r.task_id=? ORDER BY c.rowid",
            (task_id,),
        )
        learning_sources = _rows(
            db,
            "SELECT s.* FROM method_learning_sources s JOIN runs r ON r.id=s.source_run_id "
            "WHERE r.task_id=? ORDER BY s.rowid",
            (task_id,),
        )
        related_workflow_ids = {task.get("request", {}).get("workflow_id")}
        budget_ids = set(task.get("preserved_budgets", {}).values())
        for row in runs:
            run = json.loads(row["payload"])
            related_workflow_ids.update((run.get("workflow") or {}).get("method_source_ids", []))
            budget_ids.update(run.get("budget_overrides", {}).values())
        budget_ids.update(row["budget_id"] for row in calls)
        budget_ids.update(json.loads(row["payload"]).get("budget_id") for row in outcomes)
        workflows = []
        for row in _rows(db, "SELECT * FROM workflows ORDER BY rowid"):
            record = json.loads(row["payload"])
            related = (
                row["id"] in related_workflow_ids
                or record.get("source_task_id") == task_id
                or record.get("source_run_id") in run_ids
            )
            if record.get("project") == project and related:
                workflows.append(row)
                if record.get("budget_id"):
                    budget_ids.add(record["budget_id"])
        budgets = []
        ids = sorted(value for value in budget_ids if isinstance(value, str))
        for offset in range(0, len(ids), 500):
            batch = ids[offset : offset + 500]
            placeholders = ",".join("?" for _ in batch)
            budgets.extend(
                _rows(
                    db,
                    f"SELECT * FROM node_budgets WHERE id IN ({placeholders}) ORDER BY id",
                    tuple(batch),
                )
            )
        db.rollback()

    raw_tables = {
        "task": task_rows,
        "runs": runs,
        "outcomes": outcomes,
        "artifacts": artifacts,
        "events": events,
        "model_calls": calls,
        "workflows": workflows,
        "budgets": budgets,
        "learning_sources": learning_sources,
    }
    records = {
        key: [_record(row) for row in rows] for key, rows in raw_tables.items() if key != "budgets"
    }
    for call in records["model_calls"]:
        call["measurement"] = _measurement(call["record"])
    row_hashes = {
        key: [
            {
                "key": row.get("id", row.get("seq", row.get("source_run_id", row.get("node_id")))),
                "row_hash": content_hash(row),
                "payload_sha256": hashlib.sha256(row["payload"].encode("utf-8")).hexdigest()
                if "payload" in row
                else None,
            }
            for row in rows
        ]
        for key, rows in raw_tables.items()
    }
    return {
        "schema_version": 1,
        "exported_at": now(),
        "scope": {
            "kind": "development_trace",
            "project": project,
            "task_id": task_id,
            "revision": task.get("revision"),
            "single_read_transaction": True,
            "independent_sample_count": None,
            "limits": [
                "Related calls, revisions and artifacts are not independent research samples",
                "Export-time hashes do not establish historical code or reference-library versions",
            ],
        },
        "task": records.pop("task")[0],
        **records,
        "budgets": budgets,
        "manifest": {
            "hash_scope": "raw_database_rows_observed_at_export",
            "historical_execution_code_version": "unknown",
            "task_base": {
                "declared_hash": task.get("base_hash"),
                "exported_content_hash": content_hash(task.get("base_bundle", {})),
                "observation": "export_time_only",
            },
            "table_hashes": {key: content_hash(rows) for key, rows in raw_tables.items()},
            "source_row_hashes": row_hashes,
            "raw_tables_hash": content_hash(raw_tables),
        },
    }
