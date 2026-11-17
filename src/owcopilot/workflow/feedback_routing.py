"""Route explicit feedback without promoting content or operational faults to methods."""

from __future__ import annotations

import json
from typing import Literal

from ..content.hash import content_hash
from .models import now

FeedbackKind = Literal[
    "content_fact",
    "project_preference",
    "method",
    "technical_failure",
    "provider_failure",
    "unclassified",
]

_DESTINATIONS = {
    "content_fact": "content_review",
    "project_preference": "project_review",
    "method": "method_reflection",
    "technical_failure": "technical_repair",
    "provider_failure": "operational_diagnosis",
    "unclassified": "needs_evidence",
}


def route_feedback(
    store,
    project,
    family_id,
    source,
    text,
    *,
    source_kind,
    feedback_kind=None,
    feedback_event_id=None,
):
    """A missing kind denotes this explicit method endpoint, not inferred preference."""
    if source_kind not in {"author_feedback", "development_input"}:
        raise ValueError("未声明的反馈来源")
    if feedback_kind is not None and feedback_kind not in _DESTINATIONS:
        raise ValueError("未声明的反馈类型")
    requested = feedback_kind or "method"
    kind = requested
    failed = [
        node_id
        for node_id, row in (source.get("result") or {}).get("outcomes", {}).items()
        if row.get("status") in {"failed", "blocked_dependency", "cancelled"}
    ]
    provider_evidence = []
    if kind == "method" and failed:
        kind = "technical_failure"
        from .harness import HarnessStore

        cursor, harness = 0, HarnessStore(store)
        while page := harness.events(source["run_id"], after_seq=cursor, limit=1000):
            provider_evidence.extend(
                row["seq"]
                for row in page
                if row["type"] == "model/result"
                and row.get("node_id") in failed
                and row["payload"].get("status") == "failed"
            )
            cursor = page[-1]["seq"]
        if provider_evidence:
            kind = "provider_failure"
    material = {
        "project": project,
        "family_id": family_id,
        "source_run_id": source["run_id"],
        "task_id": source["task_id"],
        "task_revision": source["revision"],
        "graph_hash": source["graph_hash"],
        "feedback_hash": content_hash(text),
        "source_kind": source_kind,
        "requested_kind": requested,
        "kind": kind,
        "destination": _DESTINATIONS[kind],
        "failed_nodes": failed,
        "provider_failure_event_seqs": provider_evidence,
        "method_eligible": kind == "method",
        "author_acceptance": "unknown",
    }
    event_id = feedback_event_id or content_hash(material)
    if not isinstance(event_id, str) or not event_id.strip() or len(event_id) > 200:
        raise ValueError("反馈事件身份必需且长度不能超过200")
    key = content_hash([project, source["run_id"], event_id])
    record = {"schema_version": 1, "id": key, "feedback_event_id": event_id, **material}
    with store.connection() as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS workflow_feedback_routes_v1("
            "id TEXT PRIMARY KEY,project TEXT NOT NULL,source_run_id TEXT NOT NULL,"
            "payload TEXT NOT NULL)"
        )
        db.execute("BEGIN IMMEDIATE")
        old = db.execute(
            "SELECT payload FROM workflow_feedback_routes_v1 WHERE id=?", (key,)
        ).fetchone()
        if old:
            saved = json.loads(old[0])
            if saved.get("route_hash") != content_hash(
                {name: value for name, value in saved.items() if name != "route_hash"}
            ):
                raise ValueError("反馈分流记录完整性校验失败")
            if any(saved.get(name) != value for name, value in record.items()):
                raise ValueError("同一反馈事件不能改变来源、内容、版本或分流")
            return saved
        record["created_at"] = now()
        record["route_hash"] = content_hash(record)
        db.execute(
            "INSERT INTO workflow_feedback_routes_v1 VALUES(?,?,?,?)",
            (key, project, source["run_id"], json.dumps(record, ensure_ascii=False)),
        )
    return record
