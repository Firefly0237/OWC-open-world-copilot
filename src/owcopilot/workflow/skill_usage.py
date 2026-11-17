"""Small read-only views of the skills actually supplied to one production run."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing

from ..content.hash import content_hash
from .evolution import SourceRef
from .evolution_v2 import render_parameterized_guidance
from .execution_records import summarize_dispatches
from .graph_v2 import NodeSpecV2
from .skill_cards import SkillDefinition
from .v2_evidence import _checked, _read_trace


def run_skill_usage(store, project: str, run_id: str) -> dict:
    """Read only this run, its exact versions, and its dispatch/result records."""
    uri = store.path.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=5)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        try:
            return _snapshot(db, project, run_id)
        finally:
            db.rollback()


def _snapshot(db, project, run_id):
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "creative_v2_runs" not in tables:
        raise KeyError("制作记录不存在")
    row = db.execute(
        "SELECT payload FROM creative_v2_runs WHERE run_id=? AND project=?", (run_id, project)
    ).fetchone()
    if row is None:
        raise KeyError("制作记录不属于当前工作区或不存在")
    state = json.loads(row[0])
    trace = _read_trace(db, tables, project, run_id, [])
    if trace is None:
        raise KeyError("运行记录不存在")
    run = trace["run"]
    if (state.get("run_id"), state.get("project"), state.get("task_id"), state.get("revision")) != (
        run_id,
        project,
        run["task_id"],
        run["revision"],
    ) or content_hash(state["graph"]) != state.get("graph_hash"):
        raise ValueError("制作记录身份或工作图校验不一致")
    task = json.loads(
        db.execute("SELECT payload FROM tasks WHERE id=?", (run["task_id"],)).fetchone()[0]
    )
    measurements = summarize_dispatches(trace["requests"], trace["events"])
    responses = {}
    for event in trace["events"]:
        if event["type"] == "model/result":
            value = event["payload"]
            responses.setdefault(value.get("request_id"), {})[content_hash(value)] = value
    complete = {"creative_harness_dispatches", "creative_harness_events"} <= tables
    nodes = state["graph"]["nodes"]
    skills = []
    for binding in state.get("skill_card_bindings", []):
        version = None
        if "skill_card_versions" in tables:
            row = db.execute(
                "SELECT payload FROM skill_card_versions WHERE id=? AND project=?",
                (binding["version_id"], project),
            ).fetchone()
            version = _checked(row[0], "version_hash") if row else None
        if version is None or (
            version["card_id"] != binding["card_id"]
            or version["version_hash"] != binding["version_hash"]
            or version["definition"] != binding["definition"]
        ):
            complete = False
            skills.append(
                {
                    **{
                        key: binding[key]
                        for key in ("card_id", "version_id", "version_hash", "selection")
                    },
                    "name": "版本记录待核对",
                    "version": None,
                    "steps": [],
                }
            )
            continue
        definition = SkillDefinition.model_validate(version["definition"])
        steps = []
        for step in definition.steps:
            entry = step.entry(
                "card_" + binding["card_id"][:12],
                SourceRef(
                    kind="method", id=binding["version_id"], content_hash=binding["version_hash"]
                ),
            )
            used_nodes = []
            for node in nodes:
                if not any(
                    all(
                        tag.get(key) == binding[key]
                        for key in ("card_id", "version_id", "version_hash")
                    )
                    for tag in node.get("parameters", {}).get("skill_card_versions", [])
                ):
                    continue
                if entry.id not in {
                    item.get("id") for item in node["parameters"].get("skill_entry_templates", [])
                }:
                    continue
                status = "bound"
                for request in trace["requests"]:
                    if request["node_id"] != node["id"]:
                        continue
                    context = request["context_manifest"]
                    if context.get("graph_hash") != state["graph_hash"] or not any(
                        all(
                            tag.get(key) == binding[key]
                            for key in ("card_id", "version_id", "version_hash")
                        )
                        for tag in context.get("skill_cards", [])
                    ):
                        complete = False
                        status = "unknown"
                        continue
                    try:
                        guidance = render_parameterized_guidance(
                            NodeSpecV2.model_validate(node), context["task"]
                        )
                        user = json.loads(request["request"]["user"])
                        valid = user.get("guidance") == guidance and context.get(
                            "rendered_guidance_hash"
                        ) == content_hash(guidance)
                    except (KeyError, TypeError, ValueError):
                        valid = False
                    if not valid:
                        complete = False
                        status = "unknown"
                        continue
                    outcomes = list(responses.get(request["id"], {}).values())
                    if len(outcomes) > 1:
                        complete = False
                        status = "unknown"
                    elif status != "unknown":
                        status = (
                            "response_received"
                            if outcomes and "response" in outcomes[0]
                            else "dispatched"
                        )
                used_nodes.append({"id": node["id"], "status": status})
            statuses = {node["status"] for node in used_nodes}
            if not statuses or "unknown" in statuses:
                status = "unknown"
                complete = False
            elif statuses == {"response_received"}:
                status = "response_received"
            elif statuses != {"bound"}:
                status = "dispatched"
            else:
                status = "bound"
            steps.append(
                {
                    "id": step.id,
                    "title": step.title,
                    "operation": step.operation,
                    "node_ids": [node["id"] for node in used_nodes],
                    "nodes": used_nodes,
                    "status": status,
                }
            )
        skills.append(
            {
                **{
                    key: binding[key]
                    for key in ("card_id", "version_id", "version_hash", "selection")
                },
                "name": definition.name,
                "version": version["version"],
                "steps": steps,
            }
        )
    return {
        "schema_version": 1,
        "project": project,
        "task_id": run["task_id"],
        "run_id": run_id,
        "revision": run["revision"],
        "skills": skills,
        "coverage": "complete" if complete else "partial",
        "semantic_compliance": "unknown",
        "source": {
            "task_origin": task.get("origin_kind", "unspecified"),
            "actor_origin": "unknown",
            "collection_condition": "recorded_run",
        },
        "measurements": {
            key: measurements[key]
            for key in ("dispatch_count", "total_tokens", "usage_complete", "cost_status")
        },
    }
