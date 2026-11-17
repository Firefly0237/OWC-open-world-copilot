"""Append-only intent versions, isolated reviews and author decisions in the workflow DB."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from typing import Any, Literal

from ..content.hash import content_hash
from ..patches.models import PatchCandidate
from .intent_models import IntentContract, IntentPatchReview
from .models import Artifact, TaskRecord, new_id, now
from .store import WorkflowStore

SourceValidator = Callable[[TaskRecord, Artifact, IntentContract], None]
IntentActorKind = Literal["author", "development_operator"]


def _sealed(payload: dict[str, Any], hash_key: str) -> dict[str, Any]:
    return {**payload, hash_key: content_hash(payload)}


def _decode(payload: str, hash_key: str) -> dict[str, Any]:
    record = json.loads(payload)
    if record.get(hash_key) != content_hash(
        {key: value for key, value in record.items() if key != hash_key}
    ):
        raise ValueError("持久意图记录的hash校验失败")
    return record


class IntentStore:
    def __init__(self, workflow: WorkflowStore) -> None:
        self.workflow = workflow
        with workflow.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS creative_intent_contracts(
                    id TEXT PRIMARY KEY, project TEXT NOT NULL, task_id TEXT NOT NULL,
                    version INTEGER NOT NULL, payload TEXT NOT NULL,
                    UNIQUE(project, task_id, version));
                CREATE TABLE IF NOT EXISTS creative_intent_reviews(
                    id TEXT PRIMARY KEY, project TEXT NOT NULL, task_id TEXT NOT NULL,
                    contract_id TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS creative_intent_reviews_task
                    ON creative_intent_reviews(project, task_id);
                CREATE TABLE IF NOT EXISTS creative_intent_decisions(
                    review_id TEXT PRIMARY KEY, project TEXT NOT NULL,
                    task_id TEXT NOT NULL, payload TEXT NOT NULL);
            """)

    @staticmethod
    def _contract(db: sqlite3.Connection, project: str, contract_id: str) -> dict[str, Any]:
        row = db.execute(
            "SELECT * FROM creative_intent_contracts WHERE id=? AND project=?",
            (contract_id, project),
        ).fetchone()
        if row is None:
            raise KeyError("意图合同不属于当前项目或不存在")
        record = _decode(row["payload"], "contract_hash")
        contract = IntentContract.model_validate(record["contract"])
        if (contract.id, contract.project, contract.task_id, contract.version) != (
            row["id"],
            row["project"],
            row["task_id"],
            row["version"],
        ):
            raise ValueError("意图合同索引与内容不一致")
        return record

    @classmethod
    def _latest(cls, db: sqlite3.Connection, project: str, task_id: str) -> dict[str, Any] | None:
        row = db.execute(
            "SELECT id FROM creative_intent_contracts WHERE project=? AND task_id=? "
            "ORDER BY version DESC LIMIT 1",
            (project, task_id),
        ).fetchone()
        return cls._contract(db, project, row["id"]) if row else None

    @classmethod
    def _active_confirmed(
        cls, db: sqlite3.Connection, project: str, task_id: str
    ) -> dict[str, Any] | None:
        rows = db.execute(
            "SELECT id FROM creative_intent_contracts WHERE project=? AND task_id=? "
            "ORDER BY version DESC",
            (project, task_id),
        ).fetchall()
        for row in rows:
            record = cls._contract(db, project, row["id"])
            if record["contract"]["confirmed"]:
                return record
        return None

    @staticmethod
    def _bound_source(
        db: sqlite3.Connection,
        project: str,
        contract: IntentContract,
        expected_revision: int,
        validate: SourceValidator,
    ) -> tuple[TaskRecord, Artifact]:
        row = db.execute("SELECT payload FROM tasks WHERE id=?", (contract.task_id,)).fetchone()
        if row is None:
            raise KeyError("任务不存在")
        task = TaskRecord.model_validate_json(row["payload"])
        if task.project != project or contract.project != project:
            raise KeyError("任务不属于当前项目")
        if task.revision != expected_revision or contract.task_revision != task.revision:
            raise ValueError("任务或意图合同版本已过期")
        row = db.execute(
            "SELECT payload FROM artifacts WHERE id=?", (contract.baseline_artifact_id,)
        ).fetchone()
        if row is None:
            raise KeyError("合同基线产物不存在")
        artifact = Artifact.model_validate_json(row["payload"])
        if artifact.task_id != task.id or artifact.content_hash != contract.baseline_artifact_hash:
            raise ValueError("基线产物归属或hash不匹配")
        validate(task, artifact, contract)
        return task, artifact

    @staticmethod
    def _insert_contract(
        db: sqlite3.Connection,
        contract: IntentContract,
        *,
        action: str,
        change_reason: str,
        actor_kind: IntentActorKind | None = None,
    ) -> dict[str, Any]:
        record = _sealed(
            {
                "contract": contract.model_dump(mode="json"),
                "phase": "confirmed" if contract.confirmed else "proposed",
                "action": action,
                "change_reason": change_reason,
                "created_at": now(),
                **({"actor_kind": actor_kind} if actor_kind is not None else {}),
            },
            "contract_hash",
        )
        db.execute(
            "INSERT INTO creative_intent_contracts VALUES(?,?,?,?,?)",
            (
                contract.id,
                contract.project,
                contract.task_id,
                contract.version,
                json.dumps(record, ensure_ascii=False),
            ),
        )
        return record

    def propose(
        self,
        project: str,
        task_id: str,
        *,
        expected_revision: int,
        fields: dict[str, Any],
        supersedes_contract_id: str | None,
        expected_parent_hash: str | None,
        change_reason: str,
        validate: SourceValidator,
    ) -> dict[str, Any]:
        with self.workflow.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            latest = self._latest(db, project, task_id)
            if latest:
                previous = IntentContract.model_validate(latest["contract"])
                if (
                    supersedes_contract_id != previous.id
                    or expected_parent_hash != latest["contract_hash"]
                ):
                    raise ValueError("新意图必须明确覆盖当前最新合同及其hash")
                if not change_reason.strip():
                    raise ValueError("改变意图必须提供change_reason")
                version = previous.version + 1
            else:
                if supersedes_contract_id is not None or expected_parent_hash is not None:
                    raise ValueError("首版意图没有可覆盖的父合同")
                version = 1
            contract = IntentContract.model_validate(
                {
                    **fields,
                    "id": new_id(),
                    "project": project,
                    "task_id": task_id,
                    "task_revision": expected_revision,
                    "version": version,
                    "confirmed": False,
                    "supersedes_contract_id": supersedes_contract_id,
                }
            )
            self._bound_source(db, project, contract, expected_revision, validate)
            return self._insert_contract(
                db, contract, action="propose", change_reason=change_reason
            )

    def confirm(
        self,
        project: str,
        contract_id: str,
        *,
        expected_revision: int,
        expected_contract_hash: str,
        validate: SourceValidator,
        actor_kind: IntentActorKind = "author",
    ) -> dict[str, Any]:
        if actor_kind not in {"author", "development_operator"}:
            raise ValueError("未声明的确认操作主体")
        with self.workflow.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            record = self._contract(db, project, contract_id)
            contract = IntentContract.model_validate(record["contract"])
            latest = self._latest(db, project, contract.task_id)
            if (
                latest is None
                or latest["contract"]["id"] != contract.id
                or record["contract_hash"] != expected_contract_hash
            ):
                raise ValueError("确认目标不是当前最新合同或hash已改变")
            if contract.confirmed:
                raise ValueError("已确认合同保持不可变；修改意图请创建新proposal")
            self._bound_source(db, project, contract, expected_revision, validate)
            confirmed = IntentContract.model_validate(
                {
                    **contract.model_dump(mode="json"),
                    "id": new_id(),
                    "version": contract.version + 1,
                    "supersedes_contract_id": contract.id,
                    "confirmed": True,
                }
            )
            return self._insert_contract(
                db,
                confirmed,
                action="explicit_author_confirmation"
                if actor_kind == "author"
                else "explicit_development_operator_confirmation",
                change_reason=record["change_reason"],
                actor_kind=actor_kind,
            )

    def get_contract(self, project: str, contract_id: str) -> dict[str, Any]:
        with self.workflow.connection() as db:
            return self._contract(db, project, contract_id)

    def list_contracts(self, project: str, task_id: str) -> list[dict[str, Any]]:
        with self.workflow.connection() as db:
            row = db.execute("SELECT payload FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None or json.loads(row["payload"])["project"] != project:
                raise KeyError("任务不属于当前项目")
            rows = db.execute(
                "SELECT id FROM creative_intent_contracts WHERE project=? AND task_id=? "
                "ORDER BY version",
                (project, task_id),
            ).fetchall()
            return [self._contract(db, project, row["id"]) for row in rows]

    def review_source(
        self,
        project: str,
        contract_id: str,
        *,
        expected_revision: int,
        expected_contract_hash: str,
        validate: SourceValidator,
    ) -> tuple[IntentContract, TaskRecord, Artifact]:
        with self.workflow.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            record = self._contract(db, project, contract_id)
            contract = IntentContract.model_validate(record["contract"])
            self._require_active(db, project, record, expected_contract_hash)
            task, artifact = self._bound_source(db, project, contract, expected_revision, validate)
            return contract, task, artifact

    @classmethod
    def _require_active(
        cls,
        db: sqlite3.Connection,
        project: str,
        record: dict[str, Any],
        expected_hash: str,
    ) -> None:
        contract = IntentContract.model_validate(record["contract"])
        latest = cls._active_confirmed(db, project, contract.task_id)
        if not contract.confirmed or latest is None or latest["contract"]["id"] != contract.id:
            raise ValueError("需要当前生效、已明确确认的意图合同")
        if record["contract_hash"] != expected_hash:
            raise ValueError("意图合同hash不匹配")

    def save_review(
        self,
        project: str,
        contract: IntentContract,
        *,
        expected_contract_hash: str,
        candidate: PatchCandidate,
        report: IntentPatchReview,
        validate: SourceValidator,
    ) -> dict[str, Any]:
        with self.workflow.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            record = self._contract(db, project, contract.id)
            self._require_active(db, project, record, expected_contract_hash)
            if contract.model_dump(mode="json") != record["contract"]:
                raise ValueError("审查使用的合同内容与事务内权威版本不一致")
            self._bound_source(db, project, contract, contract.task_revision, validate)
            if (
                report.contract_id != contract.id
                or report.contract_version != contract.version
                or report.baseline_artifact_id != contract.baseline_artifact_id
            ):
                raise ValueError("审查报告与合同来源绑定不一致")
            payload = candidate.model_dump(mode="json")
            review = _sealed(
                {
                    "id": new_id(),
                    "project": project,
                    "task_id": contract.task_id,
                    "task_revision": contract.task_revision,
                    "contract_id": contract.id,
                    "contract_hash": expected_contract_hash,
                    "baseline_artifact_id": contract.baseline_artifact_id,
                    "baseline_artifact_hash": contract.baseline_artifact_hash,
                    "candidate": payload,
                    "candidate_hash": content_hash(payload),
                    "report": report.model_dump(mode="json"),
                    "created_at": now(),
                },
                "review_hash",
            )
            db.execute(
                "INSERT INTO creative_intent_reviews VALUES(?,?,?,?,?)",
                (
                    review["id"],
                    project,
                    contract.task_id,
                    contract.id,
                    json.dumps(review, ensure_ascii=False),
                ),
            )
            return review

    @staticmethod
    def _review(db: sqlite3.Connection, project: str, review_id: str) -> dict[str, Any]:
        row = db.execute(
            "SELECT payload FROM creative_intent_reviews WHERE id=? AND project=?",
            (review_id, project),
        ).fetchone()
        if row is None:
            raise KeyError("候选审查不属于当前项目或不存在")
        record = _decode(row["payload"], "review_hash")
        if content_hash(record["candidate"]) != record["candidate_hash"]:
            raise ValueError("候选hash不一致")
        return record

    def get_review(self, project: str, review_id: str) -> dict[str, Any]:
        with self.workflow.connection() as db:
            record = self._review(db, project, review_id)
            row = db.execute(
                "SELECT payload FROM creative_intent_decisions WHERE review_id=? AND project=?",
                (review_id, project),
            ).fetchone()
            return {
                **record,
                "author_decision": _decode(row["payload"], "decision_hash") if row else None,
            }

    def decide(
        self,
        project: str,
        review_id: str,
        *,
        expected_revision: int,
        expected_review_hash: str,
        decision: str,
        note: str,
        validate: SourceValidator,
        actor_kind: IntentActorKind = "author",
    ) -> dict[str, Any]:
        if actor_kind not in {"author", "development_operator"}:
            raise ValueError("未声明的决定操作主体")
        with self.workflow.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            review = self._review(db, project, review_id)
            if review["review_hash"] != expected_review_hash:
                raise ValueError("审查报告hash不匹配")
            record = self._contract(db, project, review["contract_id"])
            contract = IntentContract.model_validate(record["contract"])
            self._require_active(db, project, record, review["contract_hash"])
            self._bound_source(db, project, contract, expected_revision, validate)
            if decision not in {"accepted", "rejected"}:
                raise ValueError("作者决定仅支持accepted/rejected")
            if decision == "accepted" and review["report"]["status"] != "ready_for_author":
                raise ValueError("候选未通过技术门禁，不能接受")
            row = db.execute(
                "SELECT payload FROM creative_intent_decisions WHERE review_id=?", (review_id,)
            ).fetchone()
            if row:
                existing = _decode(row["payload"], "decision_hash")
                if existing.get("actor_kind", "author") != actor_kind:
                    raise ValueError("已记录决定的操作主体不可改变")
                if existing["decision"] != decision:
                    raise ValueError("作者决定已记录且不可覆盖")
                return existing
            result = _sealed(
                {
                    "id": new_id(),
                    "review_id": review_id,
                    "review_hash": expected_review_hash,
                    "project": project,
                    "task_id": contract.task_id,
                    "task_revision": expected_revision,
                    "contract_id": contract.id,
                    "decision": decision,
                    "note": note,
                    "action": "explicit_author_api_action"
                    if actor_kind == "author"
                    else "explicit_development_operator_api_action",
                    "actor_kind": actor_kind,
                    "created_at": now(),
                    "status": "accepted_for_review_record"
                    if decision == "accepted"
                    else "rejected_for_review_record",
                    "writes_canon": False,
                    "publishes_main_candidate": False,
                    "changes_task_revision": False,
                    "changes_method_acceptance": False,
                    "goal_status": "withheld",
                },
                "decision_hash",
            )
            db.execute(
                "INSERT INTO creative_intent_decisions VALUES(?,?,?,?)",
                (review_id, project, contract.task_id, json.dumps(result, ensure_ascii=False)),
            )
            return result
