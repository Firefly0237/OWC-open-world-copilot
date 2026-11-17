"""Immutable stage-agreement successors and their durable transition receipts."""

from __future__ import annotations

import json
from typing import Any

from ..content.hash import content_hash
from .collaboration_v2 import CollaborationPolicy
from .models import now


def change_request(
    source: dict,
    collaboration: dict,
    *,
    gate_hash: str,
    guidance: dict[str, str],
    policy: dict | None,
    expected_policy_hash: str | None,
    actor_kind: str,
) -> dict:
    if actor_kind not in {"author", "development_operator"}:
        raise ValueError("未声明的操作主体")
    node_ids = {node["id"] for node in source["graph"]["nodes"]}
    if set(guidance) - node_ids or any(
        not text.strip() or len(text) > 8000 for text in guidance.values()
    ):
        raise ValueError("修改计划需要属于当前步骤的有效指导")
    if policy is not None and expected_policy_hash is None:
        raise ValueError("修改后续约定需要当前约定版本")
    if expected_policy_hash is not None and expected_policy_hash != collaboration["policy_hash"]:
        raise ValueError("后续约定版本已变化，请刷新")
    target = CollaborationPolicy.model_validate(policy or collaboration["policy"]).normalized()
    if not guidance and (policy is None or target == collaboration["policy"]):
        raise ValueError("没有需要更新的指导或后续约定")
    payload = {
        "parent_run_id": source["run_id"],
        "project": source["project"],
        "task_id": source["task_id"],
        "task_revision": source["revision"],
        "gate_hash": gate_hash,
        "source_graph_hash": source["graph_hash"],
        "source_execution_graph_hash": source.get("execution_graph_hash"),
        "source_checkpoint_input_hash": content_hash({
            "outcomes": (source.get("result") or {}).get("outcomes", {}),
            "artifacts": (source.get("result") or {}).get("artifacts", {}),
        }),
        "source_policy_hash": collaboration["policy_hash"],
        "source_reference_hash": content_hash(source["references"]),
        "source_narrative_hash": content_hash(source.get("narrative_context")),
        "target_policy": target,
        "guidance": guidance,
        "actor_kind": actor_kind,
    }
    return {**payload, "request_hash": content_hash(payload)}


def read_change(store, project: str, parent_run_id: str, gate_hash: str) -> dict | None:
    with store.connection() as db:
        row = db.execute(
            "SELECT payload FROM creative_v2_stage_changes "
            "WHERE parent_run_id=? AND gate_hash=? AND project=?",
            (parent_run_id, gate_hash, project),
        ).fetchone()
    if row is None:
        return None
    receipt = json.loads(row[0])
    body = {key: value for key, value in receipt.items() if key != "receipt_hash"}
    if content_hash(body) != receipt.get("receipt_hash"):
        raise ValueError("后续约定交接记录校验失败")
    return receipt


def require_same_request(receipt: dict, request: dict) -> None:
    if receipt["request_hash"] != request["request_hash"]:
        raise ValueError("同一待确认项已经按其他指导、约定或操作主体处理")


def validate_pending_change(db, request: dict, source: dict) -> dict:
    """Run inside the same write transaction that saves the successor."""
    gate_row = db.execute(
        "SELECT payload FROM creative_v2_interactions WHERE run_id=? AND gate_hash=?",
        (request["parent_run_id"], request["gate_hash"]),
    ).fetchone()
    state_row = db.execute(
        "SELECT payload FROM creative_v2_collaboration WHERE run_id=? AND project=?",
        (request["parent_run_id"], request["project"]),
    ).fetchone()
    if gate_row is None or state_row is None:
        raise ValueError("待确认项已失效或不属于此运行")
    gate_record, collaboration = json.loads(gate_row[0]), json.loads(state_row[0])
    gate = gate_record["gate"]
    if (
        gate_record["status"] != "pending"
        or collaboration["policy_hash"] != request["source_policy_hash"]
        or gate["policy_hash"] != request["source_policy_hash"]
        or gate["graph_hash"] != request["source_execution_graph_hash"]
        or gate["input_hash"] != request["source_checkpoint_input_hash"]
        or gate["task_revision"] != source["revision"]
        or request["source_graph_hash"] != source["graph_hash"]
        or content_hash({key: value for key, value in gate.items() if key != "gate_hash"})
        != request["gate_hash"]
        or content_hash(source["references"]) != request["source_reference_hash"]
        or content_hash(source.get("narrative_context")) != request["source_narrative_hash"]
    ):
        raise ValueError("待确认项、策略或输入版本已经改变")
    return {"gate_record": gate_record, "collaboration": collaboration}


def write_change(db, request: dict, successor: dict, validated: dict) -> dict:
    graph = successor["graph"]
    affected = set(request["guidance"])
    edges = [*graph["data_edges"], *graph.get("control_edges", [])]
    for _ in graph["nodes"]:
        affected.update(edge["target_node"] for edge in edges if edge["source_node"] in affected)
    payload: dict[str, Any] = {
        "schema_version": 1,
        **request,
        "next_run_id": successor["run_id"],
        "target_graph_hash": successor["graph_hash"],
        "target_policy_hash": content_hash(request["target_policy"]),
        "policy_version": validated["collaboration"].get("policy_version", 1) + 1,
        "affected_nodes": sorted(affected),
        "reuse_rule": "matching_input_signature_and_artifact_hash",
        "created_at": now(),
    }
    receipt = {**payload, "receipt_hash": content_hash(payload)}
    record = validated["gate_record"]
    record.update(status="superseded", supersession={
        "next_run_id": successor["run_id"], "receipt_hash": receipt["receipt_hash"],
        "actor_kind": request["actor_kind"],
    })
    db.execute(
        "UPDATE creative_v2_interactions SET payload=? WHERE run_id=? AND gate_hash=?",
        (json.dumps(record, ensure_ascii=False), request["parent_run_id"], request["gate_hash"]),
    )
    db.execute(
        "INSERT INTO creative_v2_stage_changes VALUES(?,?,?,?,?,?)",
        (request["parent_run_id"], request["gate_hash"], request["project"],
         request["request_hash"], successor["run_id"], json.dumps(receipt, ensure_ascii=False)),
    )
    return receipt
