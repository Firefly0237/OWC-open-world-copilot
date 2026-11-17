"""Append-only selection receipts and conservative views over actual dispatches."""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..content.hash import content_hash
from .models import TaskInput, new_id, now


class SourceAxes(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    task_origin: str = "unknown"
    execution_origin: str = "unknown"
    actor_origin: str = "unknown"
    collection_condition: str = "normal_use"


class DecisionReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: int = 1
    id: str = Field(default_factory=new_id)
    project: str
    task_id: str
    task_revision: int
    input_hash: str
    policy_id: str
    policy_version: str
    observation: dict[str, Any]
    candidates: list[dict[str, Any]]
    selected_action_id: str | None
    reason: str
    budget: dict[str, Any]
    source: SourceAxes = Field(default_factory=SourceAxes)
    selection_kind: str = "deterministic"
    selection_probability: float | None = None
    created_at: str = Field(default_factory=now)


def _decode(raw: str) -> dict:
    record = json.loads(raw)
    expected = record.pop("receipt_hash")
    if content_hash(record) != expected:
        raise ValueError("选择记录完整性校验失败")
    return {**record, "receipt_hash": expected}


class ExecutionRecords:
    def __init__(self, workflow):
        self.workflow = workflow

    @staticmethod
    def _initialize(db):
        db.execute(
            "CREATE TABLE IF NOT EXISTS creative_method_decisions("
            "id TEXT PRIMARY KEY,project TEXT NOT NULL,task_id TEXT NOT NULL,payload TEXT NOT NULL)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS creative_method_decision_runs("
            "decision_id TEXT NOT NULL,run_id TEXT NOT NULL,"
            "PRIMARY KEY(decision_id,run_id))"
        )

    def append_decision(self, receipt: DecisionReceipt) -> dict:
        task = self.workflow.get_task(receipt.task_id)
        if (task.project, task.revision, content_hash(task.request)) != (
            receipt.project,
            receipt.task_revision,
            receipt.input_hash,
        ):
            raise ValueError("选择记录与当前任务合同不一致")
        if receipt.selection_kind == "deterministic" and receipt.selection_probability is not None:
            raise ValueError("确定性选择不写入伪造的探索概率")
        ids = [row["id"] for row in receipt.candidates]
        if len(ids) != len(set(ids)) or (
            receipt.selected_action_id is not None and receipt.selected_action_id not in ids
        ):
            raise ValueError("选择动作不在完整候选集合中")
        record = receipt.model_dump(mode="json")
        record["receipt_hash"] = content_hash(record)
        with self.workflow.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._initialize(db)
            current = json.loads(
                db.execute("SELECT payload FROM tasks WHERE id=?", (receipt.task_id,)).fetchone()[0]
            )
            if (
                current["project"],
                current["revision"],
                content_hash(TaskInput.model_validate(current["request"])),
            ) != (receipt.project, receipt.task_revision, receipt.input_hash):
                raise ValueError("选择记录与当前任务合同不一致")
            existing = db.execute(
                "SELECT payload FROM creative_method_decisions WHERE id=?", (receipt.id,)
            ).fetchone()
            if existing:
                if _decode(existing[0]) != record:
                    raise ValueError("已有选择记录不可改写")
                return record
            db.execute(
                "INSERT INTO creative_method_decisions VALUES(?,?,?,?)",
                (
                    receipt.id,
                    receipt.project,
                    receipt.task_id,
                    json.dumps(record, ensure_ascii=False),
                ),
            )
        return record

    def bind_run(self, project: str, decision_id: str, run_id: str) -> None:
        run = self.workflow.get_run(run_id)
        with self.workflow.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._initialize(db)
            self.bind_in_transaction(db, project, decision_id, run_id, run.task_id, run.revision)

    @staticmethod
    def bind_in_transaction(db, project, decision_id, run_id, task_id, revision):
        row = db.execute(
            "SELECT payload FROM creative_method_decisions WHERE id=? AND project=?",
            (decision_id, project),
        ).fetchone()
        if row is None:
            raise KeyError("选择记录不存在")
        receipt = _decode(row[0])
        if (task_id, revision) != (receipt["task_id"], receipt["task_revision"]):
            raise ValueError("运行不能绑定其他任务或版本的选择记录")
        db.execute(
            "INSERT OR IGNORE INTO creative_method_decision_runs VALUES(?,?)", (decision_id, run_id)
        )
        return receipt

    def for_task(self, project: str, task_id: str) -> list[dict]:
        with self.workflow.connection() as db:
            exists = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='creative_method_decisions'"
            ).fetchone()
            if not exists:
                return []
            result = []
            has_links = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='creative_method_decision_runs'"
            ).fetchone()
            for row in db.execute(
                "SELECT payload FROM creative_method_decisions "
                "WHERE project=? AND task_id=? ORDER BY rowid",
                (project, task_id),
            ):
                record = _decode(row[0])
                record["run_ids"] = (
                    [
                        item[0]
                        for item in db.execute(
                            "SELECT run_id FROM creative_method_decision_runs "
                            "WHERE decision_id=? ORDER BY rowid",
                            (record["id"],),
                        )
                    ]
                    if has_links
                    else []
                )
                record["run_links_status"] = "recorded" if has_links else "not_recorded"
                result.append(record)
            return result


def summarize_dispatches(requests: list[dict], events: list[dict]) -> dict:
    """Count one observed dispatch settlement; retries retain distinct identities."""
    by_id = {row["id"]: row for row in requests}
    if len(by_id) != len(requests):
        raise ValueError("派发记录身份重复")
    attempts = [(r["budget_id"], r["attempt"]) for r in requests]
    if len(attempts) != len(set(attempts)):
        raise ValueError("同一预算派发被登记多次")
    results: dict[str, list[dict]] = defaultdict(list)
    for event in events:
        if event["type"] == "model/result":
            results[event["payload"].get("request_id", "")].append(event["payload"])
    totals = {"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0}
    rows = []
    unknown_ids = []
    for request_id, request in by_id.items():
        outcomes = results.get(request_id, [])
        unique = {content_hash(value): value for value in outcomes}
        outcome = next(iter(unique.values())) if len(unique) == 1 else {}
        measurement = outcome.get("measurement") or {}
        usage = {key: measurement.get(key) for key in totals}
        known = (
            bool(outcome)
            and all(type(value) is int and value >= 0 for value in usage.values())
            and usage["cached_input_tokens"] <= usage["input_tokens"]
        )
        if known:
            for key, value in usage.items():
                totals[key] += value
        else:
            unknown_ids.append(request_id)
        rows.append(
            {
                "request_id": request_id,
                "logical_request_key": content_hash([request["owner"], request["request_hash"]]),
                "budget_receipt_id": request.get("budget_receipt_id"),
                "budget_id": request["budget_id"],
                "attempt": request["attempt"],
                "node_id": request["node_id"],
                "role_id": request["request"].get("role_id"),
                "status": outcome.get("status", "unknown"),
                "usage_status": "known" if known else "unknown",
                "measurement": measurement,
                "settlement_status": "conflict"
                if len(unique) > 1
                else "observed"
                if outcome
                else "missing",
                "execution_origin": request["request"]
                .get("config", {})
                .get("execution_origin", "unknown"),
            }
        )
    orphan_ids = sorted(set(results) - set(by_id))
    complete = not unknown_ids and not orphan_ids
    return {
        "schema_version": 1,
        "dispatch_count": len(requests),
        "known_usage_dispatches": len(requests) - len(unknown_ids),
        "unknown_usage_request_ids": unknown_ids,
        "orphan_result_request_ids": orphan_ids,
        "known_totals": {
            **totals,
            "uncached_input_tokens": totals["input_tokens"] - totals["cached_input_tokens"],
        },
        "total_tokens": totals["input_tokens"] + totals["output_tokens"] if complete else None,
        "cost_usd": None,
        "cost_status": "price_not_bound",
        "usage_complete": complete,
        "dispatches": rows,
        "quality": {"Q0": "unknown", "Q1": "unknown", "Q2": "unknown"},
        "training_eligible": False,
    }
