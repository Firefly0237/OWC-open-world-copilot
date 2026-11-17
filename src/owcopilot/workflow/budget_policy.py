"""Versioned dispatch grants and durable, non-replaying attempt receipts.

The ledger provides local admission and reconciliation, not provider exactly-once
delivery. A missing acknowledgement remains unknown until evidence resolves it.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..content.hash import content_hash
from .models import new_id, now

if TYPE_CHECKING:
    from .store import WorkflowStore

AttemptStatus = Literal["claimed_unknown", "completed", "completed_failed"]


class BudgetExhaustedError(ValueError):
    """A persisted grant has no remaining allowance."""


class BudgetUnknownError(BudgetExhaustedError):
    """An admitted side effect has no known outcome; reconcile before continuing."""


class BudgetMigrationRequiredError(BudgetExhaustedError):
    """The legacy database needs an explicit, backed-up schema migration."""


class BudgetPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    id: str = "prototype-governed"
    version: int = Field(default=1, ge=1)
    max_owner_dispatches: int | None = Field(default=None, ge=1)
    max_run_dispatches: int | None = Field(default=64, ge=1)
    tool_round_limit: int = Field(default=8, ge=1)
    step_attempt_limit: int = Field(default=1, ge=1)
    max_execution_attempts: int | None = Field(default=None, ge=1)
    sdk_max_retries: Literal[0] = 0


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _seal(value: dict[str, Any], field: str) -> dict[str, Any]:
    return {**value, field: content_hash(value)}


def _decode(value: str, field: str) -> dict[str, Any]:
    record = json.loads(value)
    plain = {key: item for key, item in record.items() if key != field}
    if record.get(field) != content_hash(plain):
        raise ValueError("预算账本hash不匹配")
    return record


def initialize_budget_schema(db: sqlite3.Connection) -> None:
    """Create additive ledger tables; never rebuild the legacy counter implicitly."""
    statements = (
        "CREATE TABLE IF NOT EXISTS budget_policies("
        "policy_id TEXT NOT NULL,version INTEGER NOT NULL,payload TEXT NOT NULL,"
        "PRIMARY KEY(policy_id,version))",
        "CREATE TABLE IF NOT EXISTS budget_policy_activations("
        "seq INTEGER PRIMARY KEY AUTOINCREMENT,payload TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS budget_grants("
        "seq INTEGER PRIMARY KEY AUTOINCREMENT,budget_id TEXT NOT NULL,payload TEXT NOT NULL)",
        "CREATE INDEX IF NOT EXISTS budget_grants_owner ON budget_grants(budget_id,seq)",
        "CREATE TABLE IF NOT EXISTS budget_attempt_receipts("
        "id TEXT PRIMARY KEY,kind TEXT NOT NULL,scope_key TEXT NOT NULL,"
        "owner_key TEXT NOT NULL,run_id TEXT,attempt INTEGER NOT NULL,payload TEXT NOT NULL,"
        "UNIQUE(kind,scope_key,attempt))",
        "CREATE INDEX IF NOT EXISTS budget_receipts_run ON budget_attempt_receipts(run_id)",
        "CREATE TABLE IF NOT EXISTS budget_attempt_events("
        "seq INTEGER PRIMARY KEY AUTOINCREMENT,receipt_id TEXT NOT NULL,payload TEXT NOT NULL)",
        "CREATE INDEX IF NOT EXISTS budget_events_receipt ON budget_attempt_events(receipt_id,seq)",
    )
    for statement in statements:
        db.execute(statement)
    if db.execute("SELECT 1 FROM budget_policies LIMIT 1").fetchone() is None:
        _publish_policy(db, BudgetPolicy(), "user-approved sweep31 initial policy", True)


def _publish_policy(
    db: sqlite3.Connection, policy: BudgetPolicy, reason: str, activate: bool
) -> dict[str, Any]:
    if not reason.strip() or not policy.id.strip():
        raise ValueError("预算策略需要稳定ID和变更理由")
    row = db.execute(
        "SELECT payload FROM budget_policies WHERE policy_id=? AND version=?",
        (policy.id, policy.version),
    ).fetchone()
    if row:
        record = _decode(row[0], "policy_hash")
        if record["policy"] != policy.model_dump():
            raise ValueError("已发布预算策略版本不可改写")
    else:
        record = _seal(
            {"policy": policy.model_dump(), "reason": reason, "created_at": now()},
            "policy_hash",
        )
        db.execute(
            "INSERT INTO budget_policies VALUES(?,?,?)",
            (policy.id, policy.version, _json(record)),
        )
    if activate:
        activation = _seal(
            {
                "policy_id": policy.id,
                "version": policy.version,
                "policy_hash": record["policy_hash"],
                "reason": reason,
                "created_at": now(),
            },
            "activation_hash",
        )
        db.execute("INSERT INTO budget_policy_activations(payload) VALUES(?)", (_json(activation),))
    return record


def active_policy(db: sqlite3.Connection) -> dict[str, Any]:
    row = db.execute(
        "SELECT payload FROM budget_policy_activations ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    if row is None:
        raise ValueError("没有已激活的预算策略")
    activation = _decode(row[0], "activation_hash")
    row = db.execute(
        "SELECT payload FROM budget_policies WHERE policy_id=? AND version=?",
        (activation["policy_id"], activation["version"]),
    ).fetchone()
    record = _decode(row[0], "policy_hash")
    if record["policy_hash"] != activation["policy_hash"]:
        raise ValueError("预算激活策略hash不一致")
    return record


def _grant(
    db: sqlite3.Connection, budget_id: str, policy: dict[str, Any], reason: str
) -> dict[str, Any]:
    if not budget_id.strip() or not reason.strip():
        raise ValueError("预算授权需要原身份和变更理由")
    counter = db.execute("SELECT attempts FROM node_budgets WHERE id=?", (budget_id,)).fetchone()
    grant = _seal(
        {
            "id": new_id(),
            "budget_id": budget_id,
            "policy": policy["policy"],
            "policy_hash": policy["policy_hash"],
            "reason": reason,
            "consumed_floor": int(counter[0]) if counter else 0,
            "created_at": now(),
        },
        "grant_hash",
    )
    db.execute(
        "INSERT INTO budget_grants(budget_id,payload) VALUES(?,?)", (budget_id, _json(grant))
    )
    return grant


def budget_snapshot(db: sqlite3.Connection, budget_id: str) -> dict[str, Any]:
    row = db.execute(
        "SELECT payload FROM budget_grants WHERE budget_id=? ORDER BY seq DESC LIMIT 1",
        (budget_id,),
    ).fetchone()
    grant = (
        _decode(row[0], "grant_hash")
        if row
        else _grant(
            db, budget_id, active_policy(db), "initial grant on the original budget identity"
        )
    )
    counter = db.execute("SELECT attempts FROM node_budgets WHERE id=?", (budget_id,)).fetchone()
    consumed = int(counter[0]) if counter else 0
    limit = grant["policy"]["max_owner_dispatches"]
    return {
        "budget_id": budget_id,
        "grant": grant,
        "policy": grant["policy"],
        "consumed": consumed,
        "remaining": None if limit is None else max(0, limit - consumed),
    }


def _event(
    db: sqlite3.Connection,
    receipt: dict[str, Any],
    status: AttemptStatus,
    evidence: dict[str, Any],
    *,
    reconciliation: bool = False,
) -> dict[str, Any]:
    event = _seal(
        {
            "receipt_id": receipt["id"],
            "receipt_hash": receipt["receipt_hash"],
            "status": status,
            "evidence": evidence,
            "reconciliation": reconciliation,
            "created_at": now(),
        },
        "event_hash",
    )
    db.execute(
        "INSERT INTO budget_attempt_events(receipt_id,payload) VALUES(?,?)",
        (receipt["id"], _json(event)),
    )
    return event


def receipt_state(db: sqlite3.Connection, receipt: dict[str, Any]) -> dict[str, Any]:
    rows = db.execute(
        "SELECT payload FROM budget_attempt_events WHERE receipt_id=? ORDER BY seq",
        (receipt["id"],),
    ).fetchall()
    events = [_decode(row[0], "event_hash") for row in rows]
    for event in events:
        if event["receipt_hash"] != receipt["receipt_hash"]:
            raise ValueError("预算事件没有绑定原始回执")
    return {
        **receipt,
        "status": events[-1]["status"] if events else "claimed_unknown",
        "events": events,
    }


def _new_receipt(
    db: sqlite3.Connection,
    *,
    kind: str,
    scope_key: str,
    owner_key: str,
    attempt: int,
    run_id: str | None,
    request_hash: str,
    policy: dict[str, Any],
    reason: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    receipt = _seal(
        {
            "id": new_id(),
            "kind": kind,
            "operation_key": scope_key,
            "owner_key": owner_key,
            "attempt": attempt,
            "run_id": run_id,
            "request_hash": request_hash,
            "policy": policy,
            "reason": reason,
            "metadata": metadata or {},
            "status": "claimed_unknown",
            "created_at": now(),
        },
        "receipt_hash",
    )
    db.execute(
        "INSERT INTO budget_attempt_receipts VALUES(?,?,?,?,?,?,?)",
        (receipt["id"], kind, scope_key, owner_key, run_id, attempt, _json(receipt)),
    )
    return receipt


def _table_exists(db: sqlite3.Connection, name: str) -> bool:
    return (
        db.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?", (name,)).fetchone()
        is not None
    )


def _legacy_result(record: dict[str, Any]) -> AttemptStatus:
    if record.get("outcome_known") is False:
        return "claimed_unknown"
    if record.get("status") in {"done", "discarded", "succeeded"}:
        return "completed"
    if record.get("status") == "failed":
        error = str(record.get("error", "")).lower()
        if any(word in error for word in ("timeout", "timed out", "connection", "超时", "连接")):
            return "claimed_unknown"
        if record.get("outcome_known") is True or record.get("response_received") is True:
            return "completed_failed"
        if record.get("response") is not None or any(
            word in error
            for word in (
                "badrequesterror",
                "bad request",
                "authenticationerror",
                "permissiondeniederror",
                "ratelimiterror",
                "internalservererror",
                "status_code",
                "status code",
            )
        ):
            return "completed_failed"
        # A generic historic 'failed' flag has no acknowledgement semantics.
        # Preserve uncertainty instead of inferring a remote rejection.
        return "claimed_unknown"
    return "claimed_unknown"


def import_legacy_budget(db: sqlite3.Connection, budget_id: str) -> None:
    """Backfill counted slots conservatively, without editing old counts or receipts."""
    counter = db.execute("SELECT attempts FROM node_budgets WHERE id=?", (budget_id,)).fetchone()
    if counter is None or not counter[0]:
        return
    grant = budget_snapshot(db, budget_id)["grant"]
    for attempt in range(1, int(counter[0]) + 1):
        if db.execute(
            "SELECT 1 FROM budget_attempt_receipts "
            "WHERE kind='dispatch' AND scope_key=? AND attempt=?",
            (budget_id, attempt),
        ).fetchone():
            continue
        source: dict[str, Any] = {}
        status: AttemptStatus = "claimed_unknown"
        run_id = None
        if _table_exists(db, "model_calls"):
            row = db.execute(
                "SELECT run_id,payload FROM model_calls WHERE budget_id=? AND attempt_no=?",
                (budget_id, attempt),
            ).fetchone()
            if row:
                run_id, source = row[0], json.loads(row[1])
                status = _legacy_result(source)
        if not source and _table_exists(db, "creative_harness_dispatches"):
            row = db.execute(
                "SELECT run_id,request FROM creative_harness_dispatches "
                "WHERE budget_id=? AND attempt=?",
                (budget_id, attempt),
            ).fetchone()
            if row:
                run_id, source = row[0], json.loads(row[1])
                if _table_exists(db, "creative_harness_events"):
                    rows = db.execute(
                        "SELECT payload FROM creative_harness_events "
                        "WHERE run_id=? AND event_type='model/result'",
                        (run_id,),
                    ).fetchall()
                    for item in rows:
                        result = json.loads(item[0]).get("payload", {})
                        if result.get("request_id") == source["id"]:
                            status = _legacy_result(result)
                            source = {**source, "observed_result": result}
        # A host validation failure before any provider call is known, but no matching
        # outcome is not proof of non-dispatch (e.g. process crash between commits).
        if not source and _table_exists(db, "node_outcomes"):
            for row in db.execute("SELECT run_id,payload FROM node_outcomes").fetchall():
                outcome = json.loads(row[1])
                if (
                    outcome.get("budget_id") == budget_id
                    and outcome.get("attempts") == attempt
                    and outcome.get("status")
                    in {"succeeded", "skipped_nonretryable", "retrying", "skipped_retry_exhausted"}
                ):
                    run_id, source = row[0], {"host_outcome": outcome}
                    status = "completed" if outcome["status"] == "succeeded" else "completed_failed"
                    break
        receipt = _new_receipt(
            db,
            kind="dispatch",
            scope_key=budget_id,
            owner_key=budget_id,
            attempt=attempt,
            run_id=run_id,
            request_hash=content_hash(source),
            policy=grant,
            reason="legacy counted slot imported without refund",
            metadata={"legacy": True},
        )
        _event(db, receipt, status, {"legacy_evidence": source, "counter_floor": counter[0]})


def claim_dispatch(
    db: sqlite3.Connection,
    budget_id: str,
    run_id: str,
    node_id: str,
    request: dict[str, Any],
    *,
    owner_key: str | None = None,
    reason: str = "host dispatch admission",
) -> dict[str, Any]:
    """Caller must hold BEGIN IMMEDIATE and validate authoritative run/revision first."""
    schema = db.execute("SELECT sql FROM sqlite_schema WHERE name='node_budgets'").fetchone()
    if schema and "between" in schema[0].lower():
        raise BudgetMigrationRequiredError("旧预算CHECK约束仍在；先备份并显式迁移，不能换owner绕过")
    db.execute("INSERT OR IGNORE INTO node_budgets VALUES(?,0)", (budget_id,))
    import_legacy_budget(db, budget_id)
    snapshot = budget_snapshot(db, budget_id)
    rows = db.execute(
        "SELECT payload FROM budget_attempt_receipts WHERE kind='dispatch' AND scope_key=?",
        (budget_id,),
    ).fetchall()
    if any(
        receipt_state(db, _decode(row[0], "receipt_hash"))["status"] == "claimed_unknown"
        for row in rows
    ):
        raise BudgetUnknownError("同一预算有结果未知的调用；先核对证据并对账，不能直接重放")
    if snapshot["remaining"] == 0:
        raise BudgetExhaustedError("原预算已达到当前持久授权上限")
    limit = snapshot["policy"]["max_run_dispatches"]
    count = db.execute(
        "SELECT COUNT(*) FROM budget_attempt_receipts WHERE kind='dispatch' AND run_id=?", (run_id,)
    ).fetchone()[0]
    if limit is not None and count >= limit:
        raise BudgetExhaustedError("运行已达到持久策略的模型派发上限")
    claimed = db.execute(
        "UPDATE node_budgets SET attempts=attempts+1 WHERE id=? RETURNING attempts", (budget_id,)
    ).fetchone()
    return _new_receipt(
        db,
        kind="dispatch",
        scope_key=budget_id,
        owner_key=owner_key or budget_id,
        attempt=int(claimed[0]),
        run_id=run_id,
        request_hash=content_hash(request),
        policy=snapshot["grant"],
        reason=reason,
        metadata={"node_id": node_id},
    )


def settle_dispatch(
    db: sqlite3.Connection,
    budget_id: str,
    attempt: int,
    status: AttemptStatus,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    row = db.execute(
        "SELECT payload FROM budget_attempt_receipts "
        "WHERE kind='dispatch' AND scope_key=? AND attempt=?",
        (budget_id, attempt),
    ).fetchone()
    if row is None:
        import_legacy_budget(db, budget_id)
        row = db.execute(
            "SELECT payload FROM budget_attempt_receipts "
            "WHERE kind='dispatch' AND scope_key=? AND attempt=?",
            (budget_id, attempt),
        ).fetchone()
    if row is None:
        raise KeyError("预算尝试回执不存在")
    receipt = _decode(row[0], "receipt_hash")
    current = receipt_state(db, receipt)
    if current["status"] != "claimed_unknown":
        if current["status"] != status:
            raise ValueError("已结算预算回执不可改写")
        return current
    if current["events"]:
        raise BudgetUnknownError("结果未知的派发必须通过带原回执hash的显式对账结算")
    _event(db, receipt, status, evidence)
    return receipt_state(db, receipt)


def dispatch_failure_status(exc: Exception, *, response_received: bool = False) -> AttemptStatus:
    """A lost transport acknowledgement is not an observed provider rejection."""
    if response_received:
        return "completed_failed"
    from ..llm.gateway import provider_error_info

    info = provider_error_info(exc)
    if info.category in {
        "auth",
        "permission",
        "config",
        "bad_request",
        "rate_limit",
        "conflict",
        "server_error",
        "output_budget",
    }:
        return "completed_failed"
    return "claimed_unknown"


class BudgetLedger:
    def __init__(self, workflow: WorkflowStore):
        self.workflow = workflow

    def snapshot(self, budget_id: str) -> dict[str, Any]:
        with self.workflow.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            return budget_snapshot(db, budget_id)

    def publish_policy(
        self, policy: BudgetPolicy, *, reason: str, activate: bool = True
    ) -> dict[str, Any]:
        with self.workflow.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            return _publish_policy(db, policy, reason, activate)

    def grant(self, budget_id: str, policy_id: str, version: int, *, reason: str) -> dict[str, Any]:
        with self.workflow.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT payload FROM budget_policies WHERE policy_id=? AND version=?",
                (policy_id, version),
            ).fetchone()
            if row is None:
                raise KeyError("预算策略版本不存在")
            return _grant(db, budget_id, _decode(row[0], "policy_hash"), reason)

    @staticmethod
    def _execution_state(db: sqlite3.Connection, operation_key: str) -> dict[str, Any]:
        rows = db.execute(
            "SELECT payload FROM budget_attempt_receipts "
            "WHERE kind='execution' AND scope_key=? ORDER BY attempt",
            (operation_key,),
        ).fetchall()
        attempts = [receipt_state(db, _decode(row[0], "receipt_hash")) for row in rows]
        latest = attempts[-1] if attempts else None
        return {
            "status": latest["status"] if latest else "new",
            "latest": latest,
            "attempts": attempts,
        }

    def execution_state(self, operation_key: str) -> dict[str, Any]:
        with self.workflow.connection() as db:
            return self._execution_state(db, operation_key)

    def claim_execution(
        self,
        operation_key: str,
        owner_key: str,
        request_hash: str,
        *,
        reason: str,
        legacy_state: AttemptStatus | None = None,
    ) -> dict[str, Any]:
        if not all(value.strip() for value in (operation_key, owner_key, request_hash, reason)):
            raise ValueError("执行尝试必须绑定稳定身份、请求hash和诊断理由")
        if legacy_state not in {None, "claimed_unknown", "completed", "completed_failed"}:
            raise ValueError("历史执行状态无效")
        with self.workflow.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self._execution_state(db, operation_key)
            latest = state["latest"]
            if latest and (
                latest["owner_key"] != owner_key or latest["request_hash"] != request_hash
            ):
                raise ValueError("同一执行操作不能改owner或冻结请求身份")
            if latest and state["status"] != "completed_failed":
                return {**latest, "new_dispatch": False, "admitted": False}
            policy = latest["policy"] if latest else active_policy(db)
            if latest is None and legacy_state is not None:
                imported = _new_receipt(
                    db,
                    kind="execution",
                    scope_key=operation_key,
                    owner_key=owner_key,
                    attempt=1,
                    run_id=None,
                    request_hash=request_hash,
                    policy=policy,
                    reason="legacy operation claim imported; original receipt unchanged",
                    metadata={"legacy": True},
                )
                _event(db, imported, legacy_state, {"legacy_state": legacy_state, "reason": reason})
                latest = receipt_state(db, imported)
                if legacy_state != "completed_failed":
                    return {**latest, "new_dispatch": False, "admitted": False}
            count = latest["attempt"] if latest else 0
            limit = policy["policy"]["max_execution_attempts"]
            if limit is not None and count >= limit:
                raise BudgetExhaustedError("该执行操作已达到持久策略授权上限")
            receipt = _new_receipt(
                db,
                kind="execution",
                scope_key=operation_key,
                owner_key=owner_key,
                attempt=count + 1,
                run_id=None,
                request_hash=request_hash,
                policy=policy,
                reason=reason,
            )
            return {**receipt, "new_dispatch": True, "admitted": True}

    def _finish(
        self,
        attempt_id: str,
        status: AttemptStatus,
        evidence: dict[str, Any],
        *,
        expected_hash: str | None = None,
    ) -> dict[str, Any]:
        if status not in {"completed", "completed_failed", "claimed_unknown"} or not evidence:
            raise ValueError("结算需要明确状态及真实证据")
        with self.workflow.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT payload FROM budget_attempt_receipts WHERE id=?", (attempt_id,)
            ).fetchone()
            if row is None:
                raise KeyError("执行尝试不存在")
            receipt = _decode(row[0], "receipt_hash")
            if expected_hash is not None and expected_hash != receipt["receipt_hash"]:
                raise ValueError("对账回执版本不匹配")
            state = receipt_state(db, receipt)
            if state["status"] != "claimed_unknown":
                if state["status"] != status:
                    raise ValueError("原始已结算回执不可改写")
                return state
            if state["events"] and expected_hash is None:
                raise BudgetUnknownError("已有未知结算记录必须显式对账，不能作为普通完成重写")
            _event(db, receipt, status, evidence, reconciliation=expected_hash is not None)
            return receipt_state(db, receipt)

    def finish_execution(
        self, attempt_id: str, *, status: AttemptStatus, evidence: dict[str, Any]
    ) -> dict[str, Any]:
        return self._finish(attempt_id, status, evidence)

    def reconcile_execution(
        self,
        attempt_id: str,
        *,
        expected_receipt_hash: str,
        status: AttemptStatus,
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        return self._finish(attempt_id, status, evidence, expected_hash=expected_receipt_hash)

    def reconcile_dispatch(
        self,
        receipt_id: str,
        *,
        expected_receipt_hash: str,
        status: AttemptStatus,
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        """Explicit transport-outcome reconciliation; never executes the request."""
        return self._finish(receipt_id, status, evidence, expected_hash=expected_receipt_hash)

    def dispatches(self, budget_id: str) -> list[dict[str, Any]]:
        with self.workflow.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            import_legacy_budget(db, budget_id)
            rows = db.execute(
                "SELECT payload FROM budget_attempt_receipts "
                "WHERE kind='dispatch' AND scope_key=? ORDER BY attempt",
                (budget_id,),
            ).fetchall()
            return [receipt_state(db, _decode(row[0], "receipt_hash")) for row in rows]


def migrate_budget_database(
    path: str | Path, backup_path: str | Path, *, reason: str
) -> dict[str, Any]:
    """Explicit offline-writer migration; root coordinates actual service quiescence.

    Backup is never overwritten. If later writes occur, restoring it would discard
    them; this helper deliberately does not automate restore or reset any counter.
    """
    source, destination = Path(path).resolve(), Path(backup_path).resolve()
    if not source.is_file() or destination.exists() or source == destination or not reason.strip():
        raise ValueError("迁移需要现有数据库、未占用备份路径和明确理由")
    destination.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(source, timeout=5)
    db.row_factory = sqlite3.Row
    try:
        with sqlite3.connect(destination) as backup:
            db.backup(backup)
            if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("SQLite备份完整性检查失败")
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("BEGIN IMMEDIATE")
        before = [
            tuple(row) for row in db.execute("SELECT id,attempts FROM node_budgets ORDER BY id")
        ]
        schema = db.execute("SELECT sql FROM sqlite_schema WHERE name='node_budgets'").fetchone()[0]
        if "between" in schema.lower():
            objects = db.execute(
                "SELECT sql FROM sqlite_schema WHERE tbl_name='node_budgets' "
                "AND type IN ('index','trigger') AND sql IS NOT NULL"
            ).fetchall()
            db.execute(
                "CREATE TABLE node_budgets_migrating(id TEXT PRIMARY KEY,"
                "attempts INTEGER NOT NULL CHECK(attempts>=0))"
            )
            db.execute("INSERT INTO node_budgets_migrating SELECT id,attempts FROM node_budgets")
            db.execute("DROP TABLE node_budgets")
            db.execute("ALTER TABLE node_budgets_migrating RENAME TO node_budgets")
            for row in objects:
                db.execute(row[0])
        initialize_budget_schema(db)
        for budget_id, _ in before:
            import_legacy_budget(db, budget_id)
        after = [
            tuple(row) for row in db.execute("SELECT id,attempts FROM node_budgets ORDER BY id")
        ]
        if before != after or db.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("迁移改变旧预算或破坏外键")
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise ValueError("预算迁移完整性检查失败")
        db.commit()
        return _seal(
            {
                "path": str(source),
                "backup_path": str(destination),
                "reason": reason,
                "before_hash": content_hash(before),
                "after_hash": content_hash(after),
                "budget_count": len(before),
                "integrity": integrity,
                "created_at": now(),
            },
            "migration_hash",
        )
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()
