"""Author-facing participation policy and version-bound durable interaction gates.

The host owns decisions; LangGraph owns the actual interrupt/resume checkpoint.
No model response can grant an author decision or silently promote a method.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from ..content.hash import content_hash
from .models import now
from .store import WorkflowStore

ProductionOperation = Literal[
    "plan_events",
    "draft_quest",
    "draft_logic",
    "logic_contract",
    "draft_dialogue",
    "dialogue_constrained",
    "bind_dialogue",
    "coverage_audit",
    "validate_decisions",
]

SYSTEM_CHECK_OPERATIONS = frozenset({"coverage_audit", "validate_decisions"})


class CollaborationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    mode: Literal["delegate", "direction", "edit"] = "delegate"
    plan_review: StrictBool | None = None
    key_step_review: StrictBool | None = None
    allow_read_tools: StrictBool = True
    allow_candidate_trial: StrictBool = False
    allowed_operations: list[ProductionOperation] | None = Field(default=None, max_length=32)
    review_operations: list[ProductionOperation] = Field(default_factory=list, max_length=32)

    def normalized(self) -> dict[str, Any]:
        return {
            **self.model_dump(mode="json"),
            "plan_review": self.mode in {"direction", "edit"}
            if self.plan_review is None
            else self.plan_review,
            "key_step_review": self.mode == "edit"
            if self.key_step_review is None
            else self.key_step_review,
            "allowed_operations": sorted(set(self.allowed_operations) | SYSTEM_CHECK_OPERATIONS)
            if self.allowed_operations is not None
            else None,
            "review_operations": sorted(set(self.review_operations) - SYSTEM_CHECK_OPERATIONS),
        }


_TITLES = {
    "plan_events": "梳理剧情方向",
    "draft_quest": "确认方向后展开场景",
    "logic_contract": "设计选择与游戏后果",
    "draft_logic": "设计游戏状态",
    "dialogue_constrained": "按已定后果撰写对白",
    "draft_dialogue": "撰写对白",
    "bind_dialogue": "连接对白与剧情",
    "coverage_audit": "检查实际草稿",
    "validate_decisions": "核对选择与后果",
}


class CollaborationStore:
    def __init__(self, store: WorkflowStore):
        self.store = store
        with store.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS creative_v2_collaboration(
                    run_id TEXT PRIMARY KEY, project TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS creative_v2_interactions(
                    gate_hash TEXT PRIMARY KEY, run_id TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS creative_v2_stage_changes(
                    parent_run_id TEXT NOT NULL, gate_hash TEXT NOT NULL,
                    project TEXT NOT NULL, request_hash TEXT NOT NULL,
                    next_run_id TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(parent_run_id,gate_hash));
            """)

    def initialize(
        self,
        project: str,
        run_id: str,
        policy: dict | None,
        *,
        participation: str = "delegate",
        reviewed_nodes: list[str] | None = None,
    ) -> dict:
        state = self.initial_state(
            policy, participation=participation, reviewed_nodes=reviewed_nodes
        )
        with self.store.connection() as db:
            db.execute(
                "INSERT OR IGNORE INTO creative_v2_collaboration VALUES(?,?,?)",
                (run_id, project, json.dumps(state, ensure_ascii=False)),
            )
        return self.detail(project, run_id)

    @staticmethod
    def initial_state(policy, *, participation="delegate", reviewed_nodes=None):
        normalized = CollaborationPolicy.model_validate(
            policy or {"mode": participation}
        ).normalized()
        return {
            "policy": normalized,
            "policy_hash": content_hash(normalized),
            "pause_requested": False,
            "pause_generation": 0,
            "reviewed_nodes": reviewed_nodes or [],
            "policy_version": 1,
            "created_at": now(),
        }

    def _read(self, db, project: str, run_id: str) -> dict:
        row = db.execute(
            "SELECT payload FROM creative_v2_collaboration WHERE run_id=? AND project=?",
            (run_id, project),
        ).fetchone()
        if row is None:
            raise KeyError("协作策略不存在或不属于当前项目")
        state = json.loads(row[0])
        if content_hash(state["policy"]) != state["policy_hash"]:
            raise ValueError("协作约定内容与版本校验不一致")
        return state

    def _write(self, db, run_id: str, state: dict) -> None:
        db.execute(
            "UPDATE creative_v2_collaboration SET payload=? WHERE run_id=?",
            (json.dumps(state, ensure_ascii=False), run_id),
        )

    def detail(self, project: str, run_id: str) -> dict:
        with self.store.connection() as db:
            state = self._read(db, project, run_id)
            rows = db.execute(
                "SELECT payload FROM creative_v2_interactions WHERE run_id=? ORDER BY rowid",
                (run_id,),
            ).fetchall()
        history = [json.loads(row[0]) for row in rows]
        pending = next(
            (g for g in reversed(history) if g["status"] in {"pending", "authorized"}), None
        )
        run = self.store.get_run(run_id)
        return {
            **state,
            "pending_gate": pending,
            "history": history,
            "can_pause": run.status in {"queued", "running"},
            "can_resume": run.status == "waiting_user" and pending is not None,
            "phase": "waiting_user" if pending else run.status,
            "stage_agreements": self.stage_agreements(run_id, state),
        }

    def stage_agreements(self, run_id: str, state: dict) -> list[dict]:
        """Project only the actual production roles; method permission stays separate."""
        with self.store.connection() as db:
            table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='creative_v2_runs'"
            ).fetchone()
            row = (
                db.execute(
                    "SELECT payload FROM creative_v2_runs WHERE run_id=?", (run_id,)
                ).fetchone()
                if table
                else None
            )
        operations = (
            {node["operation"] for node in json.loads(row[0])["graph"]["nodes"]} if row else set()
        )
        groups = [
            ("planning", "大纲与场景", {"plan_events", "draft_quest"}),
            ("mechanics", "选择与后果", {"draft_logic", "logic_contract"}),
            ("dialogue", "角色对白", {"draft_dialogue", "dialogue_constrained", "bind_dialogue"}),
            ("checks", "系统检查", SYSTEM_CHECK_OPERATIONS),
        ]
        policy = state["policy"]
        result = []
        for key, title, members in groups:
            present = sorted(operations & members)
            if not present:
                continue
            result.append(
                {
                    "id": key,
                    "title": title,
                    "operations": [
                        {
                            "operation": operation,
                            "title": _TITLES.get(operation, operation),
                            "allowed": self.operation_allowed(policy, operation),
                            "review_before": self.operation_review(policy, operation),
                            "system_required": operation in SYSTEM_CHECK_OPERATIONS,
                        }
                        for operation in present
                    ],
                    "policy_hash": state["policy_hash"],
                    "policy_version": state.get("policy_version", 1),
                }
            )
        result.append(
            {
                "id": "methods",
                "title": "方法试用",
                "operations": [],
                "task_permit_required": True,
                "automatic_trial_requested": policy.get("allow_candidate_trial", False),
                "policy_hash": state["policy_hash"],
                "policy_version": state.get("policy_version", 1),
            }
        )
        return result

    @staticmethod
    def operation_allowed(policy: dict, operation: str) -> bool:
        if operation in SYSTEM_CHECK_OPERATIONS:
            return True
        allowed = policy.get("allowed_operations")
        return allowed is None or operation in allowed

    @staticmethod
    def operation_review(policy: dict, operation: str) -> bool:
        if operation in SYSTEM_CHECK_OPERATIONS:
            return False
        return bool(
            operation in policy.get("review_operations", [])
            or policy.get("plan_review")
            and operation == "draft_quest"
            or policy.get("key_step_review")
            and operation
            in {"logic_contract", "draft_logic", "draft_dialogue", "dialogue_constrained"}
        )

    def request_pause(self, project: str, run_id: str, *, expected_policy_hash: str) -> dict:
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self._read(db, project, run_id)
            if state["policy_hash"] != expected_policy_hash:
                raise ValueError("协作策略已变化，请刷新")
            run = self.store.get_run(run_id)
            if run.status not in {"queued", "running", "waiting_user"}:
                raise ValueError("此运行已结束，不能暂停")
            if not state["pause_requested"]:
                state["pause_generation"] += 1
            state["pause_requested"] = True
            self._write(db, run_id, state)
            self.store._event(
                db,
                run_id,
                "collaboration_pause_requested",
                {"policy_hash": state["policy_hash"], "at": "next_node_boundary"},
            )
        return self.detail(project, run_id)

    def before_node(
        self, project: str, run_id: str, node, driver_state: dict, *, graph_hash: str
    ) -> dict | None:
        """Called outside the node exception handler and before any model side effect."""
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self._read(db, project, run_id)
            rows = db.execute(
                "SELECT payload FROM creative_v2_interactions WHERE run_id=? ORDER BY rowid DESC",
                (run_id,),
            ).fetchall()
            history = [json.loads(row[0]) for row in rows]
            # A pause is a run-wide boundary: sibling nodes must not dispatch
            # while one sibling waits for the same author decision.
            existing = next((g for g in history if g["status"] in {"pending", "authorized"}), None)
            if existing:
                return existing["gate"]
            policy = state["policy"]
            permission_required = not self.operation_allowed(policy, node.operation)
            if not permission_required and (
                node.id in state["reviewed_nodes"]
                or any(g["node_id"] == node.id and g["status"] == "consumed" for g in history)
            ):
                needs_review = False
            else:
                needs_review = self.operation_review(policy, node.operation)
            if not permission_required and not needs_review and not state["pause_requested"]:
                return None
            run = self.store.get_run(run_id)
            artifacts = driver_state.get("artifacts", {})
            available = {key: value["value"] for key, value in artifacts.items()}
            gate = {
                "run_id": run_id,
                "task_revision": run.revision,
                "node_id": node.id,
                "operation": node.operation,
                "graph_hash": graph_hash,
                "policy_hash": state["policy_hash"],
                "policy_version": state.get("policy_version", 1),
                "permission_required": permission_required,
                "input_hash": content_hash(driver_state),
                "pause_generation": state["pause_generation"],
                "stage": "planning" if node.operation == "draft_quest" else "execution",
                "title": _TITLES.get(node.operation, node.operation),
                "reason": "此步骤尚未获执行许可，请调整后续约定或停止。"
                if permission_required
                else "你请求在下一步开始前暂停"
                if state["pause_requested"]
                else "按参与方式等待你的意见",
                "available_outputs": available,
            }
            gate["gate_hash"] = content_hash(gate)
            record = {
                "gate": gate,
                "gate_hash": gate["gate_hash"],
                "node_id": node.id,
                "status": "pending",
                "created_at": now(),
                "decision": None,
            }
            db.execute(
                "INSERT INTO creative_v2_interactions VALUES(?,?,?)",
                (gate["gate_hash"], run_id, json.dumps(record, ensure_ascii=False)),
            )
            self.store._event(db, run_id, "collaboration_gate_opened", gate)
        return gate

    def authorize_resume(
        self,
        project: str,
        run_id: str,
        *,
        expected_gate_hash: str,
        actor_kind: Literal["author", "development_operator"] = "author",
    ) -> dict:
        if actor_kind not in {"author", "development_operator"}:
            raise ValueError("未声明的恢复操作主体")
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self._read(db, project, run_id)
            row = db.execute(
                "SELECT payload FROM creative_v2_interactions WHERE gate_hash=? AND run_id=?",
                (expected_gate_hash, run_id),
            ).fetchone()
            if row is None:
                raise ValueError("待决门已过期或不属于此运行")
            record = json.loads(row[0])
            run = self.store.get_run(run_id)
            task = self.store.get_task(run.task_id)
            gate = record["gate"]
            if gate.get("permission_required"):
                raise ValueError("此步骤未获执行许可，请调整后续约定，不能直接继续")
            if (
                run.cancel_requested
                or run.revision != task.revision
                or run.status != "waiting_user"
            ):
                raise ValueError("运行已取消、任务已变化或当前没有待恢复中断")
            if state["policy_hash"] != gate["policy_hash"]:
                raise ValueError("待决门策略版本已过期")
            if record["status"] == "authorized":
                if record["decision"].get("actor_kind", "author") != actor_kind:
                    raise ValueError("同一待决门不能改写原操作主体")
                return record["decision"]
            if record["status"] != "pending":
                raise ValueError("此待决门已经处理")
            decision = {
                "gate_hash": expected_gate_hash,
                "action": "continue",
                "authority": "explicit_author_action"
                if actor_kind == "author"
                else "development_operator_action",
                "actor_kind": actor_kind,
                "created_at": now(),
            }
            decision["decision_hash"] = content_hash(decision)
            record.update(status="authorized", decision=decision)
            db.execute(
                "UPDATE creative_v2_interactions SET payload=? WHERE gate_hash=?",
                (json.dumps(record, ensure_ascii=False), expected_gate_hash),
            )
        return decision

    def consume_resume(self, project: str, run_id: str, gate: dict, decision: dict) -> None:
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self._read(db, project, run_id)
            row = db.execute(
                "SELECT payload FROM creative_v2_interactions WHERE gate_hash=? AND run_id=?",
                (gate["gate_hash"], run_id),
            ).fetchone()
            record = json.loads(row[0]) if row else None
            if (
                record is None
                or record["status"] not in {"authorized", "consumed"}
                or record["decision"] != decision
            ):
                raise ValueError("恢复值没有对应服务端作者决定")
            if state["policy_hash"] != gate["policy_hash"]:
                raise ValueError("恢复策略已过期")
            if record["status"] == "consumed":
                return  # sibling replay of the same immutable authorization
            record["status"] = "consumed"
            db.execute(
                "UPDATE creative_v2_interactions SET payload=? WHERE gate_hash=?",
                (json.dumps(record, ensure_ascii=False), gate["gate_hash"]),
            )
            if state["pause_generation"] == gate["pause_generation"]:
                state["pause_requested"] = False
            self._write(db, run_id, state)
            self.store._event(db, run_id, "collaboration_resumed", decision)

    def resume_value(self, project: str, run_id: str) -> dict | None:
        detail = self.detail(project, run_id)
        gate = detail["pending_gate"]
        return gate["decision"] if gate and gate["status"] == "authorized" else None
