"""Project-local durable runs, immutable artifacts, events and bounded node budgets."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ..content.hash import content_hash
from .budget_policy import (
    BudgetExhaustedError,
    budget_snapshot,
    claim_dispatch,
    initialize_budget_schema,
    settle_dispatch,
)
from .models import Artifact, NodeOutcome, RunRecord, TaskRecord, new_id, now


class RunInterruptedError(ValueError):
    """A cancelled or superseded run cannot reserve another dispatch."""


class WorkflowStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs(
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS runs_task ON runs(task_id);
                CREATE TABLE IF NOT EXISTS artifacts(
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS artifacts_task ON artifacts(task_id);
                CREATE TABLE IF NOT EXISTS node_budgets(
                    id TEXT PRIMARY KEY, attempts INTEGER NOT NULL CHECK(attempts>=0));
                CREATE TABLE IF NOT EXISTS node_outcomes(
                    run_id TEXT NOT NULL, node_id TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(run_id,node_id));
                CREATE TABLE IF NOT EXISTS events(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                    event_type TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS events_run ON events(run_id,seq);
                CREATE TABLE IF NOT EXISTS workflows(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS previews(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS preferences(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS method_learning_sources(
                    source_run_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS model_calls(
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, budget_id TEXT NOT NULL,
                    attempt_no INTEGER NOT NULL, payload TEXT NOT NULL,
                    UNIQUE(budget_id,attempt_no));
            """)
            db.execute("BEGIN IMMEDIATE")
            initialize_budget_schema(db)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=5.0)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def save_task(self, task: TaskRecord, *, expected_revision: int | None = None) -> None:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT payload FROM tasks WHERE id=?", (task.id,)).fetchone()
            if expected_revision is not None and (
                current is None or json.loads(current[0])["revision"] != expected_revision
            ):
                raise ValueError("任务版本已改变，请读取最新版本")
            db.execute(
                "INSERT OR REPLACE INTO tasks VALUES(?,?)", (task.id, task.model_dump_json())
            )

    def get_task(self, task_id: str) -> TaskRecord:
        with self.connection() as db:
            row = db.execute("SELECT payload FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError("任务不存在")
        return TaskRecord.model_validate_json(row[0])

    def list_tasks(self) -> list[dict[str, Any]]:
        with self.connection() as db:
            rows = db.execute("SELECT payload FROM tasks ORDER BY rowid DESC LIMIT 50").fetchall()
        return [json.loads(row[0]) for row in rows]

    def create_run(self, task: TaskRecord) -> tuple[RunRecord, bool]:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT payload FROM runs WHERE task_id=?", (task.id,)).fetchall()
            for row in reversed(rows):
                old = RunRecord.model_validate_json(row[0])
                if old.revision == task.revision and old.status in {
                    "queued",
                    "running",
                    "waiting_user",
                }:
                    return old, False
            run = RunRecord(task_id=task.id, revision=task.revision)
            db.execute("INSERT INTO runs VALUES(?,?,?)", (run.id, task.id, run.model_dump_json()))
            self._event(db, run.id, "run_created", {"run_id": run.id})
        return run, True

    def get_run(self, run_id: str) -> RunRecord:
        with self.connection() as db:
            row = db.execute("SELECT payload FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError("运行不存在")
        return RunRecord.model_validate_json(row[0])

    def update_run(self, run_id: str, **changes: Any) -> RunRecord:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError("运行不存在")
            payload = json.loads(row[0])
            payload.update(changes)
            run = RunRecord.model_validate(payload)
            db.execute("UPDATE runs SET payload=? WHERE id=?", (run.model_dump_json(), run.id))
            self._event(db, run.id, "run_updated", changes)
        return run

    def list_runs(self, task_id: str) -> list[RunRecord]:
        with self.connection() as db:
            rows = db.execute(
                "SELECT payload FROM runs WHERE task_id=? ORDER BY rowid DESC", (task_id,)
            ).fetchall()
        return [RunRecord.model_validate_json(row[0]) for row in rows]

    def attempts(self, budget_id: str) -> int:
        with self.connection() as db:
            row = db.execute(
                "SELECT attempts FROM node_budgets WHERE id=?", (budget_id,)
            ).fetchone()
        return int(row[0]) if row else 0

    def claim_attempt(
        self, budget_id: str, run_id: str, node_id: str, *, operation: str | None = None
    ) -> int:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            run_row = db.execute("SELECT payload FROM runs WHERE id=?", (run_id,)).fetchone()
            if run_row is None:
                raise KeyError("运行不存在")
            run = RunRecord.model_validate_json(run_row[0])
            task_row = db.execute("SELECT payload FROM tasks WHERE id=?", (run.task_id,)).fetchone()
            if (
                run.cancel_requested
                or run.status in {"cancelled", "interrupted"}
                or task_row is None
                or json.loads(task_row[0])["revision"] != run.revision
            ):
                raise RunInterruptedError("运行已取消或输入版本改变，未领取新尝试")
            if operation is None and run.workflow is not None:
                operation = next(
                    (node.operation for node in run.workflow.nodes if node.id == node_id), None
                )
            operation = operation or node_id
            receipt = claim_dispatch(db, budget_id, run_id, node_id, {"operation": operation})
            attempt = int(receipt["attempt"])
            running = NodeOutcome(
                node_id=node_id,
                operation=operation,
                status="running",
                attempts=attempt,
                budget_id=budget_id,
            )
            # Reserving a dispatch and identifying its logical operation are one commit.
            # A revision must see this even before the runtime publishes its start event.
            db.execute(
                "INSERT OR REPLACE INTO node_outcomes VALUES(?,?,?)",
                (run_id, node_id, running.model_dump_json()),
            )
            self._event(
                db,
                run_id,
                "node_attempt",
                {
                    "node_id": node_id,
                    "operation": operation,
                    "budget_id": budget_id,
                    "attempt": attempt,
                    "budget_receipt_id": receipt["id"],
                    "budget_policy": receipt["policy"],
                },
            )
        return attempt

    def settle_attempt(
        self, budget_id: str, attempt: int, *, status: str, evidence: dict[str, Any]
    ) -> None:
        from typing import cast

        from .budget_policy import AttemptStatus

        if status not in {"claimed_unknown", "completed", "completed_failed"}:
            raise ValueError("预算结算状态无效")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            settle_dispatch(db, budget_id, attempt, cast(AttemptStatus, status), evidence)

    @staticmethod
    def _preserved_failure_budgets(
        db: sqlite3.Connection, task_id: str, existing: dict[str, str]
    ) -> dict[str, str]:
        preserved = dict(existing)
        seen: set[str] = set()
        rows = db.execute(
            "SELECT outcomes.payload FROM runs AS run "
            "JOIN node_outcomes AS outcomes ON outcomes.run_id=run.id "
            "WHERE run.task_id=? ORDER BY run.rowid DESC",
            (task_id,),
        ).fetchall()
        for row in rows:
            outcome = NodeOutcome.model_validate_json(row[0])
            if outcome.operation in seen:
                continue
            if outcome.status == "succeeded":
                seen.add(outcome.operation)
                preserved.pop(outcome.operation, None)
            elif outcome.attempts:
                seen.add(outcome.operation)
                preserved[outcome.operation] = outcome.budget_id
        return preserved

    def preserved_failure_budgets(self, task: TaskRecord) -> dict[str, str]:
        with self.connection() as db:
            return self._preserved_failure_budgets(db, task.id, task.preserved_budgets)

    def complete_node(
        self,
        run_id: str,
        outcome: NodeOutcome,
        artifact: Artifact | None = None,
    ) -> None:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError("运行不存在")
            run = RunRecord.model_validate_json(row[0])
            task_row = db.execute("SELECT payload FROM tasks WHERE id=?", (run.task_id,)).fetchone()
            stale = task_row is None or json.loads(task_row[0])["revision"] != run.revision
            if run.cancel_requested or stale:
                outcome = outcome.model_copy(
                    update={
                        "status": "cancelled",
                        "artifact_id": None,
                        "error": "已取消或输入版本改变，迟到产物未发布",
                    }
                )
                artifact = None
            if artifact is not None:
                db.execute(
                    "INSERT INTO artifacts VALUES(?,?,?)",
                    (
                        artifact.id,
                        artifact.task_id,
                        artifact.model_dump_json(),
                    ),
                )
            db.execute(
                "INSERT OR REPLACE INTO node_outcomes VALUES(?,?,?)",
                (
                    run_id,
                    outcome.node_id,
                    outcome.model_dump_json(),
                ),
            )
            self._event(db, run_id, "node_outcome", outcome.model_dump(mode="json"))

    def outcomes(self, run_id: str) -> dict[str, NodeOutcome]:
        with self.connection() as db:
            rows = db.execute(
                "SELECT node_id,payload FROM node_outcomes WHERE run_id=?",
                (run_id,),
            ).fetchall()
        return {row[0]: NodeOutcome.model_validate_json(row[1]) for row in rows}

    def get_artifact(self, artifact_id: str) -> Artifact:
        with self.connection() as db:
            row = db.execute("SELECT payload FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
        if row is None:
            raise KeyError("产物不存在")
        return Artifact.model_validate_json(row[0])

    def artifacts(self, task_id: str) -> list[Artifact]:
        with self.connection() as db:
            rows = db.execute(
                "SELECT payload FROM artifacts WHERE task_id=? ORDER BY rowid",
                (task_id,),
            ).fetchall()
        return [Artifact.model_validate_json(row[0]) for row in rows]

    def append_artifact(self, artifact: Artifact) -> None:
        with self.connection() as db:
            db.execute(
                "INSERT INTO artifacts VALUES(?,?,?)",
                (
                    artifact.id,
                    artifact.task_id,
                    artifact.model_dump_json(),
                ),
            )

    def commit_revision(
        self,
        task: TaskRecord,
        *,
        expected_revision: int,
        artifact: Artifact | None = None,
        preference: dict[str, Any] | None = None,
    ) -> None:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM tasks WHERE id=?", (task.id,)).fetchone()
            if row is None or json.loads(row[0])["revision"] != expected_revision:
                raise ValueError("任务版本已改变，请读取最新版本")
            # The action's earlier snapshot can be stale: a worker may have claimed a
            # dispatch between that read and this transaction. Merge authority under
            # the same write lock as the revision, before cancelling old runs.
            current = TaskRecord.model_validate_json(row[0])
            task.preserved_budgets = self._preserved_failure_budgets(
                db, task.id, {**current.preserved_budgets, **task.preserved_budgets}
            )
            db.execute("UPDATE tasks SET payload=? WHERE id=?", (task.model_dump_json(), task.id))
            if artifact is not None:
                db.execute(
                    "INSERT INTO artifacts VALUES(?,?,?)",
                    (
                        artifact.id,
                        artifact.task_id,
                        artifact.model_dump_json(),
                    ),
                )
            if preference is not None:
                record = {
                    **preference,
                    "id": new_id(),
                    "disabled": False,
                    "created_at": now(),
                }
                db.execute(
                    "INSERT INTO preferences VALUES(?,?)",
                    (
                        record["id"],
                        json.dumps(record, ensure_ascii=False),
                    ),
                )
            rows = db.execute("SELECT id,payload FROM runs WHERE task_id=?", (task.id,)).fetchall()
            for run_row in rows:
                run = RunRecord.model_validate_json(run_row[1])
                if run.status in {"queued", "running", "waiting_user"}:
                    run.cancel_requested = True
                    if run.status != "running":
                        run.status = "cancelled"
                    db.execute(
                        "UPDATE runs SET payload=? WHERE id=?",
                        (
                            run.model_dump_json(),
                            run.id,
                        ),
                    )
                    self._event(db, run.id, "revision_changed", {"revision": task.revision})

    def events(self, run_id: str, after_seq: int = 0) -> list[dict[str, Any]]:
        with self.connection() as db:
            rows = db.execute(
                "SELECT * FROM events WHERE run_id=? AND seq>? ORDER BY seq LIMIT 500",
                (run_id, after_seq),
            ).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    def begin_model_call(
        self,
        run_id: str,
        budget_id: str,
        node_id: str,
        attempt_no: int,
        request: dict[str, Any],
    ) -> str:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            # Admission happened in claim_attempt, including the persisted per-run
            # cap. This unique receipt can start exactly one provider dispatch.
            receipt_row = db.execute(
                "SELECT id FROM budget_attempt_receipts WHERE kind='dispatch' "
                "AND scope_key=? AND attempt=? AND run_id=?",
                (budget_id, attempt_no, run_id),
            ).fetchone()
            if receipt_row is None:
                raise BudgetExhaustedError("模型派发没有匹配的持久预算回执")
            call_id = new_id()
            record = {
                "id": call_id,
                "node_id": node_id,
                "attempt": attempt_no,
                "request": request,
                "created_at": now(),
                "status": "running",
                "budget_receipt_id": receipt_row[0],
                "budget_policy": budget_snapshot(db, budget_id)["grant"],
            }
            try:
                db.execute(
                    "INSERT INTO model_calls VALUES(?,?,?,?,?)",
                    (
                        call_id,
                        run_id,
                        budget_id,
                        attempt_no,
                        json.dumps(record, ensure_ascii=False),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise BudgetExhaustedError("单次节点尝试不允许隐藏的第二次模型派发") from exc
        return call_id

    def finish_model_call(
        self,
        call_id: str,
        *,
        response: str | None = None,
        error: str = "",
        measurement: dict[str, Any] | None = None,
        outcome_known: bool = True,
    ) -> None:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT budget_id,attempt_no,payload FROM model_calls WHERE id=?", (call_id,)
            ).fetchone()
            if row is None:
                raise KeyError("模型调用记录不存在")
            record = {
                **json.loads(row[2]),
                "response": response,
                "error": error,
                "status": "failed" if error else "done",
                "finished_at": now(),
                "outcome_known": outcome_known,
            }
            if measurement is not None:
                record["measurement"] = measurement
            db.execute(
                "UPDATE model_calls SET payload=? WHERE id=?",
                (
                    json.dumps(record, ensure_ascii=False),
                    call_id,
                ),
            )
            settle_dispatch(
                db,
                row[0],
                int(row[1]),
                "claimed_unknown"
                if not outcome_known
                else "completed_failed"
                if error
                else "completed",
                {
                    "model_call_id": call_id,
                    "error": error,
                    "response_received": response is not None,
                },
            )

    def model_calls(self, run_id: str) -> list[dict[str, Any]]:
        with self.connection() as db:
            rows = db.execute(
                "SELECT payload FROM model_calls WHERE run_id=? ORDER BY rowid",
                (run_id,),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def _event(
        self,
        db: sqlite3.Connection,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        db.execute(
            "INSERT INTO events(run_id,event_type,payload,created_at) VALUES(?,?,?,?)",
            (run_id, event_type, json.dumps(payload, ensure_ascii=False), now()),
        )

    def save_workflow(self, payload: dict[str, Any]) -> dict[str, Any]:
        record = {"id": new_id(), "created_at": now(), **payload}
        with self.connection() as db:
            db.execute(
                "INSERT INTO workflows VALUES(?,?)",
                (
                    record["id"],
                    json.dumps(record, ensure_ascii=False),
                ),
            )
        return record

    def workflows(self) -> list[dict[str, Any]]:
        with self.connection() as db:
            rows = db.execute("SELECT payload FROM workflows ORDER BY rowid DESC").fetchall()
        return [json.loads(row[0]) for row in rows]

    def learning_source(self, source_run_id: str) -> dict[str, Any] | None:
        with self.connection() as db:
            row = db.execute(
                "SELECT payload FROM method_learning_sources WHERE source_run_id=?",
                (source_run_id,),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def freeze_learning_source(self, source_run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT OR IGNORE INTO method_learning_sources VALUES(?,?)",
                (source_run_id, json.dumps(payload, ensure_ascii=False)),
            )
            row = db.execute(
                "SELECT payload FROM method_learning_sources WHERE source_run_id=?",
                (source_run_id,),
            ).fetchone()
        return json.loads(row[0])

    def induced_workflow(self, source_run_id: str) -> dict[str, Any] | None:
        return next(
            (
                row
                for row in self.workflows()
                if row.get("source_run_id") == source_run_id
                and row.get("memory_kind") == "trace_induced"
            ),
            None,
        )

    def save_induced_workflow(
        self, source_run_id: str, artifact_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Commit a source-bound method once; late task revisions cannot publish it."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            run_row = db.execute("SELECT payload FROM runs WHERE id=?", (source_run_id,)).fetchone()
            if run_row is None:
                raise KeyError("源运行不存在")
            run = RunRecord.model_validate_json(run_row[0])
            task_row = db.execute("SELECT payload FROM tasks WHERE id=?", (run.task_id,)).fetchone()
            if task_row is None:
                raise RunInterruptedError("源任务不存在")
            task = TaskRecord.model_validate_json(task_row[0])
            if task.revision != run.revision or run.cancel_requested or run.status != "completed":
                raise RunInterruptedError("源版本已改变，归纳结果未发布")
            if payload.get("project") != task.project:
                raise ValueError("归纳结果项目范围不符")
            artifact_row = db.execute(
                "SELECT payload FROM artifacts WHERE id=?", (artifact_id,)
            ).fetchone()
            if artifact_row is None:
                raise ValueError("归纳结果缺少实际产物")
            artifact = Artifact.model_validate_json(artifact_row[0])
            if (
                artifact.task_id != task.id
                or artifact.run_id != run.id
                or artifact.revision != task.revision
                or artifact.kind != "learned_method"
            ):
                raise ValueError("归纳产物与源运行不符")
            source_row = db.execute(
                "SELECT payload FROM method_learning_sources WHERE source_run_id=?", (run.id,)
            ).fetchone()
            if source_row is None or json.loads(source_row[0])["trace_hash"] != payload.get(
                "source_trace_hash"
            ):
                raise ValueError("归纳输入快照与结果不符")
            if artifact.payload.get("trace_hash") != payload.get(
                "source_trace_hash"
            ) or content_hash(artifact.payload.get("spec")) != content_hash(payload.get("spec")):
                raise ValueError("方法记录不匹配实际归纳产物")
            rows = db.execute("SELECT payload FROM workflows ORDER BY rowid DESC").fetchall()
            for row in rows:
                previous = json.loads(row[0])
                if (
                    previous.get("source_run_id") == run.id
                    and previous.get("memory_kind") == "trace_induced"
                ):
                    return previous
            record = {**payload, "id": new_id(), "created_at": now()}
            db.execute(
                "INSERT INTO workflows VALUES(?,?)",
                (record["id"], json.dumps(record, ensure_ascii=False)),
            )
        return record

    def set_method_acceptance(
        self, workflow_id: str, *, project: str, expected_hash: str, status: str
    ) -> dict[str, Any]:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM workflows WHERE id=?", (workflow_id,)).fetchone()
            if row is None:
                raise KeyError("方法不存在")
            record = json.loads(row[0])
            if record.get("project") != project:
                raise KeyError("方法不属于当前项目")
            if content_hash(record) != expected_hash:
                raise ValueError("方法版本已改变，请重新读取")
            if (
                record.get("memory_kind") != "trace_induced"
                or record.get("method_origin") != "trace_induced"
                or record.get("technical_status") != "validated"
            ):
                raise ValueError("只有技术校验后的实际归纳方法可以确认接受")
            if status not in {"pending", "accepted", "rejected"}:
                raise ValueError("方法接受状态无效")
            record.update(
                acceptance_status=status,
                acceptance_record={
                    "status": status,
                    "source": "explicit_workflow_patch",
                    "at": now(),
                    "content_acceptance": "unknown",
                },
            )
            db.execute(
                "UPDATE workflows SET payload=? WHERE id=?",
                (json.dumps(record, ensure_ascii=False), workflow_id),
            )
        return record

    def update_workflow(self, workflow_id: str, *, disabled: bool) -> dict[str, Any]:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM workflows WHERE id=?", (workflow_id,)).fetchone()
            if row is None:
                raise KeyError("流程不存在")
            record = {**json.loads(row[0]), "disabled": disabled}
            db.execute(
                "UPDATE workflows SET payload=? WHERE id=?",
                (
                    json.dumps(record, ensure_ascii=False),
                    workflow_id,
                ),
            )
        return record

    def revise_workflow(
        self, workflow_id: str, *, expected_hash: str, changes: dict[str, Any]
    ) -> dict[str, Any]:
        """Create an immutable successor and disable its predecessor in one transaction."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM workflows WHERE id=?", (workflow_id,)).fetchone()
            if row is None:
                raise KeyError("流程不存在")
            previous = json.loads(row[0])
            if content_hash(previous) != expected_hash:
                raise ValueError("流程版本已改变，请重新读取")
            record = {
                **previous,
                **changes,
                "id": new_id(),
                "created_at": now(),
                "source_workflow_id": workflow_id,
                "version": int(previous.get("version", 1)) + 1,
                "disabled": False,
            }
            db.execute(
                "INSERT INTO workflows VALUES(?,?)",
                (record["id"], json.dumps(record, ensure_ascii=False)),
            )
            db.execute(
                "UPDATE workflows SET payload=? WHERE id=?",
                (json.dumps({**previous, "disabled": True}, ensure_ascii=False), workflow_id),
            )
        return record

    def add_preference(self, payload: dict[str, Any]) -> dict[str, Any]:
        record = {**payload, "id": new_id(), "disabled": False, "created_at": now()}
        with self.connection() as db:
            db.execute(
                "INSERT INTO preferences VALUES(?,?)",
                (
                    record["id"],
                    json.dumps(record, ensure_ascii=False),
                ),
            )
        return record

    def preferences(self) -> list[dict[str, Any]]:
        with self.connection() as db:
            rows = db.execute("SELECT payload FROM preferences ORDER BY rowid").fetchall()
        return [json.loads(row[0]) for row in rows]

    def disable_preference(self, preference_id: str) -> None:
        with self.connection() as db:
            row = db.execute(
                "SELECT payload FROM preferences WHERE id=?", (preference_id,)
            ).fetchone()
            if row is None:
                raise KeyError("偏好不存在")
            record = {**json.loads(row[0]), "disabled": True}
            db.execute(
                "UPDATE preferences SET payload=? WHERE id=?",
                (
                    json.dumps(record, ensure_ascii=False),
                    preference_id,
                ),
            )

    def save_preview(
        self,
        preview_id: str,
        payload: dict[str, Any],
        *,
        expected_seq: int | None = None,
        task_id: str | None = None,
        expected_revision: int | None = None,
    ) -> None:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if task_id is not None:
                task_row = db.execute("SELECT payload FROM tasks WHERE id=?", (task_id,)).fetchone()
                if task_row is None or json.loads(task_row[0])["revision"] != expected_revision:
                    raise ValueError("预演源内容版本已改变，请重新开始预演")
            row = db.execute("SELECT payload FROM previews WHERE id=?", (preview_id,)).fetchone()
            if expected_seq is not None and (
                row is None or json.loads(row[0])["session"]["action_seq"] != expected_seq
            ):
                raise ValueError("预演状态已改变，请刷新后再选择")
            db.execute(
                "INSERT OR REPLACE INTO previews VALUES(?,?)",
                (
                    preview_id,
                    json.dumps(payload, ensure_ascii=False),
                ),
            )

    def get_preview(self, preview_id: str) -> dict[str, Any]:
        with self.connection() as db:
            row = db.execute("SELECT payload FROM previews WHERE id=?", (preview_id,)).fetchone()
        if row is None:
            raise KeyError("预演不存在")
        return json.loads(row[0])
