"""Read-only export of source-bound v2 execution and independent author decisions."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing

from ..content.hash import content_hash
from .execution_records import summarize_dispatches
from .models import RunRecord, TaskRecord, now

_PAGE_SIZE = 1000


def _pages(db, sql: str, parameters: tuple = ()) -> Iterator[sqlite3.Row]:
    cursor = db.execute(sql, parameters)
    while page := cursor.fetchmany(_PAGE_SIZE):
        yield from page


def _checked(raw: str, key: str) -> dict:
    record = json.loads(raw)
    if record.get(key) != content_hash(
        {name: value for name, value in record.items() if name != key}
    ):
        raise ValueError("导出关联记录校验失败")
    return record


def _task_links(db, project: str, task_id: str) -> dict:
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    relevant = (
        "creative_edit_preview_forks",
        "creative_original_preview_forks",
        "previews",
        "method_trial_scopes",
        "method_trial_scope_events",
        "method_trial_scope_claims",
        "method_trial_scope_results",
        "method_trial_scope_grants",
        "creative_v2_stage_changes",
        "creative_v2_collaboration",
    )
    result: dict = {
        "coverage": {name: "available" if name in tables else "not_recorded" for name in relevant},
        "edit_preview_forks": [],
        "original_preview_forks": [],
        "derived_task_previews": [],
        "method_trial_scopes": [],
        "method_trial_events": [],
        "method_trial_claims": [],
        "method_trial_results": [],
        "method_trial_grants": [],
        "stage_changes": [],
        "policy_versions": [],
    }
    if "creative_edit_preview_forks" in tables:
        result["edit_preview_forks"] = [
            _checked(row[0], "migration_hash")
            for row in db.execute(
                "SELECT payload FROM creative_edit_preview_forks WHERE project=? "
                "AND json_extract(payload,'$.result.project')=? "
                "AND json_extract(payload,'$.result.task_id')=? ORDER BY rowid",
                (project, project, task_id),
            )
        ]
    if "previews" in tables:
        result["derived_task_previews"] = [
            json.loads(row[0])
            for row in db.execute(
                "SELECT payload FROM previews WHERE json_extract(payload,'$.project')=? "
                "AND json_extract(payload,'$.task_id')=? "
                "AND (json_extract(payload,'$.application_id') IS NOT NULL "
                "OR json_extract(payload,'$.version_mode')='fixed_historical') ORDER BY rowid",
                (project, task_id),
            )
        ]
        if "creative_original_preview_forks" in tables:
            result["original_preview_forks"] = [
                _checked(row[0], "migration_hash")
                for row in db.execute(
                    "SELECT f.payload FROM creative_original_preview_forks f "
                    "JOIN previews p ON p.id=json_extract(f.payload,'$.preview_id') "
                    "WHERE f.project=? AND json_extract(p.payload,'$.project')=? "
                    "AND json_extract(p.payload,'$.task_id')=? ORDER BY f.rowid",
                    (project, project, task_id),
                )
            ]
    if "method_trial_scopes" in tables:
        result["method_trial_scopes"] = [
            _checked(row[0], "permit_hash")
            for row in db.execute(
                "SELECT payload FROM method_trial_scopes WHERE project=? "
                "AND json_extract(payload,'$.project')=? AND json_extract(payload,'$.task_id')=? "
                "ORDER BY rowid",
                (project, project, task_id),
            )
        ]
        for table, output, hash_key in (
            ("method_trial_scope_events", "method_trial_events", "event_hash"),
            ("method_trial_scope_claims", "method_trial_claims", "claim_hash"),
            ("method_trial_scope_results", "method_trial_results", "receipt_hash"),
        ):
            if table not in tables:
                continue
            project_clause = "" if table == "method_trial_scope_events" else " AND child.project=?"
            parameters = (project, project, task_id) + (() if not project_clause else (project,))
            result[output] = [
                _checked(row[0], hash_key)
                for row in db.execute(
                    f"SELECT child.payload FROM {table} child "
                    "JOIN method_trial_scopes scope ON scope.id=child.scope_id "
                    "WHERE scope.project=? AND json_extract(scope.payload,'$.project')=? "
                    "AND json_extract(scope.payload,'$.task_id')=?"
                    + project_clause
                    + " ORDER BY child.rowid",
                    parameters,
                )
            ]
        if "method_trial_scope_grants" in tables:
            result["method_trial_grants"] = [
                dict(row)
                for row in db.execute(
                    "SELECT grant_row.operation_id,grant_row.request_hash,grant_row.scope_id "
                    "FROM method_trial_scope_grants grant_row JOIN method_trial_scopes scope "
                    "ON scope.id=grant_row.scope_id WHERE grant_row.project=? AND scope.project=? "
                    "AND json_extract(scope.payload,'$.project')=? "
                    "AND json_extract(scope.payload,'$.task_id')=? ORDER BY grant_row.rowid",
                    (project, project, project, task_id),
                )
            ]
    if "creative_v2_stage_changes" in tables:
        result["stage_changes"] = [
            _checked(row[0], "receipt_hash")
            for row in db.execute(
                "SELECT payload FROM creative_v2_stage_changes WHERE project=? "
                "AND json_extract(payload,'$.project')=? AND json_extract(payload,'$.task_id')=? "
                "ORDER BY rowid",
                (project, project, task_id),
            )
        ]
    if "creative_v2_collaboration" in tables:
        result["policy_versions"] = [
            json.loads(row[0])
            for row in db.execute(
                "SELECT c.payload FROM creative_v2_collaboration c JOIN runs r ON r.id=c.run_id "
                "WHERE c.project=? AND r.task_id=? ORDER BY c.rowid",
                (project, task_id),
            )
        ]
    return result


def _payloads(db, tables, table, where="", parameters=(), *, hash_key=None):
    if table not in tables:
        return []
    sql = f"SELECT payload FROM {table}" + (" WHERE " + where if where else "") + " ORDER BY rowid"
    return [
        _checked(row[0], hash_key) if hash_key else json.loads(row[0])
        for row in _pages(db, sql, parameters)
    ]


def _read_execution(db, tables, project, run, task, missing):
    rows = _payloads(db, tables, "creative_v2_runs", "run_id=?", (run["id"],))
    if not rows:
        missing.append({"section": "execution", "id": run["id"], "reason": "snapshot_not_recorded"})
        return {"task_id": task["id"], "run_id": run["id"], "run": run, "graph": None}
    state = rows[0]
    if (state.get("run_id"), state.get("project"), state.get("task_id"), state.get("revision")) != (
        run["id"],
        project,
        task["id"],
        run["revision"],
    ):
        raise ValueError("冻结运行的索引与项目／任务版本不一致")
    if content_hash(state["graph"]) != state.get("graph_hash"):
        raise ValueError("冻结工作图hash不一致")
    if any(content_hash(ref["body"]) != ref["body_hash"] for ref in state.get("references", [])):
        raise ValueError("冻结参考内容hash不一致")
    state.update(
        run=run,
        spec_hash=state["graph_hash"],
        execution_graph_hash=(state.get("result") or {}).get("graph_hash"),
    )
    policies = _payloads(
        db, tables, "creative_v2_collaboration", "run_id=? AND project=?", (run["id"], project)
    )
    if policies:
        policy = policies[0]
        if content_hash(policy["policy"]) != policy["policy_hash"]:
            raise ValueError("协作约定内容与版本校验不一致")
        state["collaboration"] = {
            **policy,
            "history": _payloads(db, tables, "creative_v2_interactions", "run_id=?", (run["id"],)),
        }
    state["requirements_report"] = (
        {
            row[0]: json.loads(row[1])
            for row in _pages(
                db,
                "SELECT node_id,payload FROM creative_v2_requirement_reports WHERE run_id=?",
                (run["id"],),
            )
        }
        if "creative_v2_requirement_reports" in tables
        else {}
    )
    changes = _payloads(
        db,
        tables,
        "creative_v2_stage_changes",
        "next_run_id=? AND project=?",
        (run["id"], project),
        hash_key="receipt_hash",
    )
    state["stage_change"] = changes[0] if changes else None
    state["method_trials"] = _payloads(
        db, tables, "creative_v2_trials", "project=? AND source_run_id=?", (project, run["id"])
    )
    state["method_trials"].extend(
        _payloads(
            db,
            tables,
            "creative_v2_trial_results_v2",
            "project=? AND (json_extract(payload,'$.source_run_id')=? "
            "OR json_extract(payload,'$.run_id')=?)",
            (project, run["id"], run["id"]),
        )
    )
    return state


def _read_skill(db, tables, project, related, missing):
    from .evolution import EvolutionStore

    result = {
        "family": None,
        "versions": [],
        "experiences": [],
        "episodes": [],
        "state_history": [],
        "evolution_traces": [],
        "continuation_receipts": [],
    }
    if not related:
        return result
    if not {"evolution_versions", "evolution_families", "evolution_states"} <= tables:
        missing.append({"section": "skill", "id": related, "reason": "tables_not_recorded"})
        return result
    try:
        version = EvolutionStore._version(db, project, related)
        family = EvolutionStore._family(db, project, version["family_id"])
    except KeyError:
        missing.append({"section": "skill", "id": related, "reason": "linked_record_missing"})
        return result
    result["family"] = family
    for row in _pages(
        db,
        "SELECT id FROM evolution_versions WHERE project=? AND family_id=? ORDER BY version",
        (project, family["id"]),
    ):
        value = EvolutionStore._version(db, project, row[0])
        state_exists = db.execute(
            "SELECT 1 FROM evolution_states WHERE project=? AND version_id=?",
            (project, row[0]),
        ).fetchone()
        value["state"] = EvolutionStore._state(db, project, row[0]) if state_exists else None
        if not state_exists:
            missing.append(
                {"section": "skill_state", "id": row[0], "reason": "linked_record_missing"}
            )
        result["versions"].append(value)
    result["experiences"] = _payloads(
        db,
        tables,
        "evolution_experiences",
        "project=? AND family_id=?",
        (project, family["id"]),
        hash_key="experience_hash",
    )
    result["episodes"] = _payloads(
        db,
        tables,
        "evolution_episodes",
        "project=? AND family_id=?",
        (project, family["id"]),
        hash_key="episode_hash",
    )
    for episode in result["episodes"]:
        outcomes = _payloads(
            db,
            tables,
            "evolution_episode_results",
            "project=? AND episode_id=?",
            (project, episode["id"]),
            hash_key="result_hash",
        )
        episode["result"] = outcomes[0] if outcomes else None
    episode_ids = {row["id"] for row in result["episodes"]}
    result["continuation_receipts"] = [
        row
        for row in _payloads(db, tables, "evolution_continuation_results", "project=?", (project,))
        if row.get("episode_id") in episode_ids
    ]
    result["state_history"] = [
        _checked(row[0], "state_hash")
        for row in _pages(
            db,
            "SELECT s.payload FROM evolution_states s "
            "JOIN evolution_versions v ON v.id=s.version_id "
            "WHERE s.project=? AND v.project=? AND v.family_id=? ORDER BY s.seq",
            (project, project, family["id"]),
        )
    ]
    return result


def _read_decisions(db, tables, project, task_id, missing):
    result = _payloads(
        db,
        tables,
        "creative_method_decisions",
        "project=? AND task_id=?",
        (project, task_id),
        hash_key="receipt_hash",
    )
    for record in result:
        if (record.get("project"), record.get("task_id")) != (project, task_id):
            raise ValueError("选择记录索引与任务不一致")
        record["run_ids"] = (
            [
                row[0]
                for row in _pages(
                    db,
                    "SELECT run_id FROM creative_method_decision_runs "
                    "WHERE decision_id=? ORDER BY rowid",
                    (record["id"],),
                )
            ]
            if "creative_method_decision_runs" in tables
            else []
        )
    if result and "creative_method_decision_runs" not in tables:
        missing.append({"section": "decisions", "reason": "run_links_not_recorded"})
    return result


def _read_trace(db, tables, project, run_id, missing):
    row = db.execute("SELECT payload FROM runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        missing.append({"section": "trace", "id": run_id, "reason": "linked_run_missing"})
        return None
    run = RunRecord.model_validate_json(row[0]).model_dump(mode="json")
    owner = db.execute("SELECT payload FROM tasks WHERE id=?", (run["task_id"],)).fetchone()
    if owner is None or json.loads(owner[0])["project"] != project:
        raise ValueError("关联运行不属于当前项目或缺少任务")
    events = []
    if "creative_harness_events" in tables:
        for row in _pages(
            db,
            "SELECT seq,payload FROM creative_harness_events WHERE run_id=? ORDER BY seq",
            (run_id,),
        ):
            event = _checked(row[1], "event_hash")
            if event["run_id"] != run_id:
                raise ValueError("Harness事件的运行身份不一致")
            events.append({"seq": row[0], **event})
    requests = []
    if "creative_harness_dispatches" in tables:
        for row in _pages(
            db,
            "SELECT id,request FROM creative_harness_dispatches WHERE run_id=? ORDER BY rowid",
            (run_id,),
        ):
            request = json.loads(row[1])
            if (request.get("id"), request.get("run_id")) != (row[0], run_id) or (
                content_hash(request["request"]) != request["request_hash"]
                or content_hash(request["context_manifest"]) != request["context_hash"]
            ):
                raise ValueError("Harness请求身份／上下文hash不匹配")
            requests.append(request)
    return {"run": run, "events": events, "requests": requests}


def _run_owner(db, project, run_id):
    row = db.execute(
        "SELECT r.task_id,json_extract(t.payload,'$.project') FROM runs r "
        "LEFT JOIN tasks t ON t.id=r.task_id WHERE r.id=?",
        (run_id,),
    ).fetchone()
    if row is None:
        return None
    if row[1] != project:
        raise ValueError("Skill关联运行不属于当前项目或缺少来源任务")
    return row[0]


def _read_procedural_skills(db, tables, project, task_id, state, missing):
    table_keys = {
        "skill_cards": "card_hash",
        "skill_card_versions": "version_hash",
        "skill_card_events": "event_hash",
        "skill_card_uses": "use_hash",
    }
    result = {
        "scope": "selected_run_bindings_and_current_task_derived_candidates_only",
        "coverage": {
            table: "available" if table in tables else "not_recorded" for table in table_keys
        },
        "bindings": [],
        "cards": [],
        "head_version_scope": [],
        "versions": [],
        "events": [],
        "uses": [],
        "consumption": [],
        "summary_sources": [],
        "summary_attempts": [],
        "summary_traces": [],
        "author_quality": "unknown",
        "transfer_benefit": "unknown",
        "binding_is_consumption": False,
        "consumption_is_semantic_compliance": False,
    }
    bindings = state.get("skill_card_bindings", [])
    if not {"skill_cards", "skill_card_versions"} <= tables:
        if bindings:
            missing.append({"section": "procedural_skills", "reason": "linked_tables_not_recorded"})
        return result
    task_runs = {row[0] for row in _pages(db, "SELECT id FROM runs WHERE task_id=?", (task_id,))}
    versions = {}
    for binding in bindings:
        identity = {
            key: binding[key] for key in ("card_id", "version_id", "version_hash", "selection")
        }
        result["bindings"].append(identity)
        values = _payloads(
            db,
            tables,
            "skill_card_versions",
            "project=? AND id=?",
            (project, binding["version_id"]),
            hash_key="version_hash",
        )
        if not values:
            missing.append(
                {
                    "section": "skill_version",
                    "id": binding["version_id"],
                    "reason": "linked_record_missing",
                }
            )
            continue
        version = values[0]
        if (version["card_id"], version["version_hash"], version["project"]) != (
            binding["card_id"],
            binding["version_hash"],
            project,
        ):
            raise ValueError("Skill选用与冻结版本身份不一致")
        versions[version["id"]] = version
    for row in _pages(
        db,
        "SELECT v.payload FROM skill_card_versions v JOIN runs r "
        "ON r.id=json_extract(v.payload,'$.source.run_id') "
        "WHERE v.project=? AND r.task_id=? AND json_extract(v.payload,'$.source.kind')='run' "
        "ORDER BY v.rowid",
        (project, task_id),
    ):
        version = _checked(row[0], "version_hash")
        if version["project"] != project or version["source"].get("task_id", task_id) != task_id:
            raise ValueError("Skill候选来源与任务索引不一致")
        versions[version["id"]] = version
    result["versions"] = list(versions.values())
    for card_id in sorted({version["card_id"] for version in versions.values()}):
        heads = _payloads(
            db,
            tables,
            "skill_cards",
            "project=? AND id=?",
            (project, card_id),
            hash_key="card_hash",
        )
        if not heads:
            missing.append(
                {"section": "skill_card", "id": card_id, "reason": "linked_record_missing"}
            )
        else:
            if (heads[0]["id"], heads[0]["project"]) != (card_id, project):
                raise ValueError("Skill卡片与项目索引不一致")
            result["cards"].append(heads[0])
            result["head_version_scope"].append(
                {
                    "card_id": card_id,
                    "current_version_included": heads[0]["current_version_id"] in versions,
                    "active_version_included": heads[0]["active_version_id"] in versions,
                }
            )
        card_versions = {key for key, value in versions.items() if value["card_id"] == card_id}
        for event in _payloads(
            db,
            tables,
            "skill_card_events",
            "project=? AND card_id=?",
            (project, card_id),
            hash_key="event_hash",
        ):
            if event["card_id"] != card_id:
                raise ValueError("Skill事件与卡片身份不一致")
            payload = event["payload"]
            event_run, source_run = payload.get("run_id"), payload.get("source", {}).get("run_id")
            version_id = payload.get("version_id") or payload.get("parent_version_id")
            control = event["kind"] in {"enabled", "disabled"} and not version_id and not event_run
            related = version_id in card_versions and (not event_run or event_run in task_runs)
            if event["kind"] in {"duplicate_evidence", "no_change"}:
                related = related and source_run in task_runs
            if not (control or related):
                continue
            result["events"].append(event)
            if event["kind"] == "consumed" and event_run in task_runs:
                result["consumption"].append(event)
        for use in _payloads(
            db,
            tables,
            "skill_card_uses",
            "project=? AND json_extract(payload,'$.card_id')=?",
            (project, card_id),
            hash_key="use_hash",
        ):
            if use.get("run_id") in task_runs and use.get("version_id") in card_versions:
                if use.get("project") != project or use.get("card_id") != card_id:
                    raise ValueError("Skill使用记录与项目／卡片身份不一致")
                result["uses"].append(use)
    if versions:
        for table in {"skill_card_events", "skill_card_uses"} - tables:
            missing.append(
                {"section": "procedural_skills", "id": table, "reason": "linked_table_not_recorded"}
            )
    for version in versions.values():
        source = version["source"]
        if source.get("kind") != "run":
            continue
        source_run = source.get("run_id")
        owner = _run_owner(db, project, source_run) if source_run else None
        if owner and source.get("task_id", owner) != owner:
            raise ValueError("Skill来源任务与实际运行不一致")
        current = owner == task_id
        summary_id = source.get("summary_run_id")
        if current and not summary_id:
            missing.append(
                {
                    "section": "skill_summary",
                    "id": version["id"],
                    "reason": "trace_reference_not_recorded",
                }
            )
        if not owner:
            missing.append(
                {
                    "section": "skill_source",
                    "id": source_run or version["id"],
                    "reason": "linked_run_missing",
                }
            )
        result["summary_sources"].append(
            {
                "version_id": version["id"],
                "source_run_id": source_run,
                "source_task_id": owner,
                "summary_run_id": summary_id,
                "cost_scope": "current_task_learning"
                if current
                else "cross_task_provenance_only"
                if owner
                else "unresolved_source_not_costed",
                "trace_included": False,
                "author_quality": "unknown",
                "summary_execution_receipt_id": source.get("summary_execution_receipt_id"),
            }
        )
    return result


def _read_summary_links(db, tables, project, task_id, procedural):
    attempts = {}

    def link(run_id, source_id, receipt_id, evidence, version_id=None):
        if not run_id:
            return
        if _run_owner(db, project, source_id) != task_id:
            raise ValueError("Skill总结来源不属于当前任务")
        owner = _run_owner(db, project, run_id)
        if owner is not None and owner != task_id:
            raise ValueError("Skill总结运行与来源任务不一致")
        record = attempts.setdefault(
            run_id,
            {
                "summary_run_id": run_id,
                "source_run_id": source_id,
                "summary_execution_receipt_id": None,
                "candidate_version_ids": [],
                "link_evidence": [],
                "cost_scope": "current_task_learning",
                "trace_included": False,
                "summary_status": "unknown",
                "author_quality": "unknown",
            },
        )
        if record["source_run_id"] != source_id or (
            receipt_id and record["summary_execution_receipt_id"] not in {None, receipt_id}
        ):
            raise ValueError("Skill总结来源或执行回执存在冲突")
        if receipt_id:
            record["summary_execution_receipt_id"] = receipt_id
        if version_id and version_id not in record["candidate_version_ids"]:
            record["candidate_version_ids"].append(version_id)
        if evidence not in record["link_evidence"]:
            record["link_evidence"].append(evidence)

    for source in procedural["summary_sources"]:
        if source["cost_scope"] == "current_task_learning":
            link(
                source["summary_run_id"],
                source["source_run_id"],
                source["summary_execution_receipt_id"],
                "skill_version_source",
                source["version_id"],
            )
    if "creative_harness_dispatches" in tables:
        for row in _pages(
            db,
            "SELECT d.run_id,d.request FROM creative_harness_dispatches d JOIN runs source "
            "ON source.id=json_extract(d.request,'$.context_manifest.source_run_id') "
            "WHERE source.task_id=? "
            "AND json_extract(d.request,'$.context_manifest.purpose')='procedural_skill_candidate' "
            "ORDER BY d.rowid",
            (task_id,),
        ):
            request = json.loads(row[1])
            context = request["context_manifest"]
            if request["request"].get("role_id") != "skill_summary":
                raise ValueError("Skill总结上下文与派发角色不一致")
            link(
                row[0],
                context["source_run_id"],
                context.get("summary_execution_receipt_id"),
                "harness_request_context",
            )
    if "creative_harness_events" in tables:
        for row in _pages(
            db,
            "SELECT e.run_id,e.payload FROM creative_harness_events e JOIN runs source "
            "ON source.id=json_extract(e.payload,'$.payload.source_run_id') "
            "WHERE source.task_id=? "
            "AND e.event_type IN ('skill/summary_result','skill/summary_failed') "
            "ORDER BY e.seq",
            (task_id,),
        ):
            event = _checked(row[1], "event_hash")
            if event["run_id"] != row[0]:
                raise ValueError("Skill总结事件与运行索引不一致")
            link(
                row[0],
                event["payload"]["source_run_id"],
                event["payload"].get("summary_execution_receipt_id"),
                "summary_event",
            )
    return list(attempts.values())


def _source_exclusions(decisions, dispatches):
    exclusions = []
    for receipt in decisions:
        source = receipt.get("source", {})
        reasons = [
            "training_permission_and_outcome_not_attested",
            "an_operation_record_is_not_a_consented_author_evaluation",
        ]
        exclusions.append(
            {
                "kind": "decision",
                "id": receipt["id"],
                "source": source,
                "excluded_from": ["training", "verified_author_evaluation"],
                "reasons": reasons,
            }
        )
    for dispatch in dispatches:
        origin = dispatch.get("execution_origin", "unknown")
        exclusions.append(
            {
                "kind": "dispatch",
                "id": dispatch["request_id"],
                "execution_origin": origin,
                "excluded_from": ["training", "verified_real_provider_samples"],
                "reasons": [
                    "training_permission_and_outcome_not_attested",
                    "transport_attestation_not_recorded",
                ],
            }
        )
    return exclusions


def _read_budget(db, tables, requests, episode_ids, missing, summary_receipts=()):
    receipts = {}
    for request in requests:
        receipt_id = request.get("budget_receipt_id")
        values = (
            _payloads(
                db,
                tables,
                "budget_attempt_receipts",
                "id=?",
                (receipt_id,),
                hash_key="receipt_hash",
            )
            if receipt_id
            else []
        )
        if not values:
            missing.append(
                {
                    "section": "budget",
                    "id": request["id"],
                    "reason": "dispatch_receipt_not_recorded",
                }
            )
            continue
        receipt = values[0]
        if (
            receipt["id"],
            receipt["kind"],
            receipt["run_id"],
            receipt["attempt"],
            receipt["operation_key"],
        ) != (receipt_id, "dispatch", request["run_id"], request["attempt"], request["budget_id"]):
            raise ValueError("派发与预算回执身份不一致")
        receipts[receipt["id"]] = receipt
    for episode_id in sorted(episode_ids):
        for receipt in _payloads(
            db,
            tables,
            "budget_attempt_receipts",
            "kind='execution' AND scope_key=?",
            ("method-episode:" + episode_id,),
            hash_key="receipt_hash",
        ):
            receipts[receipt["id"]] = receipt
    for reference in summary_receipts:
        receipt_id = reference["id"]
        values = _payloads(
            db, tables, "budget_attempt_receipts", "id=?", (receipt_id,), hash_key="receipt_hash"
        )
        if not values:
            missing.append(
                {
                    "section": "skill_summary_budget",
                    "id": receipt_id,
                    "reason": "linked_execution_receipt_missing",
                }
            )
            continue
        receipt = values[0]
        owners = {
            content_hash(
                {key: value for key, value in request["owner"].items() if key != "legacy_budget_id"}
            )
            for request in requests
            if request["run_id"] == reference["summary_run_id"]
        }
        if receipt["kind"] != "execution" or not receipt["operation_key"].startswith(
            "skill-summary:"
        ):
            raise ValueError("Skill总结预算不是正式总结执行回执")
        if owners and receipt["owner_key"] not in owners:
            raise ValueError("Skill总结执行预算与实际派发归属不一致")
        if not owners:
            missing.append(
                {
                    "section": "skill_summary_budget",
                    "id": receipt_id,
                    "reason": "dispatch_owner_not_recorded",
                }
            )
        receipts[receipt_id] = receipt
    events = [
        event
        for receipt_id in receipts
        for event in _payloads(
            db,
            tables,
            "budget_attempt_events",
            "receipt_id=?",
            (receipt_id,),
            hash_key="event_hash",
        )
    ]
    grants = [
        grant
        for budget_id in sorted({row["budget_id"] for row in requests})
        for grant in _payloads(
            db, tables, "budget_grants", "budget_id=?", (budget_id,), hash_key="grant_hash"
        )
    ]
    return {"attempt_receipts": list(receipts.values()), "events": events, "grants": grants}


def export_v2_evidence(store, root, project, run_id):
    # One read transaction, including linked traces. No schema initialization or model calls.
    uri = store.path.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=5.0)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        try:
            result = _export_snapshot(db, project, run_id)
        finally:
            db.rollback()
    from .skill_policy import offline_maintenance_trajectories

    result["maintenance_trajectories"] = offline_maintenance_trajectories(result)
    return {**result, "export_hash": content_hash(result)}


def _export_snapshot(db, project, run_id):
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if (
        not {"runs", "tasks"} <= tables
        or db.execute(
            "SELECT r.id FROM runs r JOIN tasks t ON t.id=r.task_id "
            "WHERE r.id=? AND json_extract(t.payload,'$.project')=?",
            (run_id, project),
        ).fetchone()
        is None
    ):
        raise KeyError("运行不存在")
    missing = []
    trace = _read_trace(db, tables, project, run_id, missing)
    if trace is None:
        raise KeyError("运行不存在")
    task_row = db.execute(
        "SELECT payload FROM tasks WHERE id=?", (trace["run"]["task_id"],)
    ).fetchone()
    task = TaskRecord.model_validate_json(task_row[0]).model_dump(mode="json")
    state = _read_execution(db, tables, project, trace["run"], task, missing)
    related = state.get("skill_version_id") or (state.get("saved_skill") or {}).get("version_id")
    skill = _read_skill(db, tables, project, related, missing)
    procedural = _read_procedural_skills(db, tables, project, task["id"], state, missing)
    decisions = _read_decisions(db, tables, project, task["id"], missing)
    intent = {
        key: _payloads(db, tables, table, "project=? AND task_id=?", (project, task["id"]))
        for key, table in (
            ("contracts", "creative_intent_contracts"),
            ("reviews", "creative_intent_reviews"),
            ("decisions", "creative_intent_decisions"),
            ("applications", "creative_intent_applications"),
        )
    }
    intent["ai_proposals"] = _payloads(
        db,
        tables,
        "creative_intent_ai_proposals",
        "project=? AND json_extract(payload,'$.task_id')=?",
        (project, task["id"]),
    )
    intent["task_links"] = _task_links(db, project, task["id"])
    traces = {run_id: trace}

    def read(run_key):
        if not run_key:
            return None
        if run_key not in traces:
            value = _read_trace(db, tables, project, run_key, missing)
            if value is None:
                return None
            traces[run_key] = value
        return traces[run_key]

    summary_receipts = []
    procedural["summary_attempts"] = _read_summary_links(
        db, tables, project, task["id"], procedural
    )
    for attempt in procedural["summary_attempts"]:
        summary_id = attempt["summary_run_id"]
        value = read(summary_id)
        if value:
            attempt["trace_included"] = True
            attempt["summary_status"] = value["run"]["status"]
            procedural["summary_traces"].append(value)
        receipt_id = attempt["summary_execution_receipt_id"]
        if receipt_id:
            summary_receipts.append({"id": receipt_id, "summary_run_id": summary_id})
        else:
            missing.append(
                {
                    "section": "skill_summary_budget",
                    "id": summary_id,
                    "reason": "execution_receipt_reference_not_recorded",
                }
            )
        for source in procedural["summary_sources"]:
            if source["summary_run_id"] == summary_id:
                source["trace_included"] = attempt["trace_included"]
                source["summary_execution_receipt_id"] = receipt_id
    procedural["summary_traces"] = list(
        {row["run"]["id"]: row for row in procedural["summary_traces"]}.values()
    )

    planning = []
    for node in (state.get("graph") or {}).get("nodes", []):
        value = read(node.get("parameters", {}).get("initial_planning", {}).get("run_id"))
        if value and value not in planning:
            planning.append(value)
    for continuation in skill["continuation_receipts"]:
        value = read(continuation.get("trace_run_id"))
        if value:
            skill["evolution_traces"].append(value)
    episode_ids = {row["id"] for row in skill["episodes"]}
    if episode_ids and "creative_harness_events" in tables:
        for row in _pages(
            db,
            "SELECT DISTINCT e.run_id FROM creative_harness_events e "
            "JOIN evolution_episodes episode "
            "ON episode.id=json_extract(e.payload,'$.payload.episode_id') "
            "WHERE episode.project=? AND episode.family_id=? ORDER BY e.run_id",
            (project, skill["family"]["id"]),
        ):
            value = read(row[0])
            if value:
                skill["evolution_traces"].append(value)
    skill["evolution_traces"] = list(
        {row["run"]["id"]: row for row in skill["evolution_traces"]}.values()
    )
    intent["proposal_traces"] = []
    for proposal in intent["ai_proposals"]:
        value = read(proposal.get("trace_run_id"))
        if value and value not in intent["proposal_traces"]:
            intent["proposal_traces"].append(value)
    decision_traces = []
    for decision in decisions:
        for linked_id in decision["run_ids"]:
            value = read(linked_id)
            if value and value not in decision_traces:
                decision_traces.append(value)
    requests = {
        request["id"]: request for value in traces.values() for request in value["requests"]
    }
    events = {
        (event["run_id"], event["seq"]): event
        for value in traces.values()
        for event in value["events"]
    }
    measurements = summarize_dispatches(list(requests.values()), list(events.values()))
    measurements["scope"] = "exported_unique_dispatches_including_related_method_history"
    task_run_ids = {key for key, value in traces.items() if value["run"]["task_id"] == task["id"]}
    task_measurements = summarize_dispatches(
        [request for request in requests.values() if request["run_id"] in task_run_ids],
        [event for event in events.values() if event["run_id"] in task_run_ids],
    )
    task_measurements["scope"] = "unique_exported_dispatches_owned_by_current_task"
    task_measurements["excluded_cross_task_skill_source_traces"] = sorted(
        {
            source["summary_run_id"]
            for source in procedural["summary_sources"]
            if source["cost_scope"] == "cross_task_provenance_only" and source["summary_run_id"]
        }
    )
    budget = _read_budget(
        db, tables, list(requests.values()), episode_ids, missing, summary_receipts
    )
    required_tables = {
        "creative_v2_runs",
        "creative_harness_events",
        "creative_harness_dispatches",
        "creative_method_decisions",
        "creative_method_decision_runs",
    }
    artifacts = _payloads(db, tables, "artifacts", "task_id=?", (task["id"],))
    manifest = {
        "schema_version": 1,
        "scope": "selected_run_and_linked_traces_task_intent_artifacts_and_related_method_family",
        "read_consistency": "single_sqlite_read_transaction",
        "page_size": _PAGE_SIZE,
        "truncated": False,
        "counts": {
            "runs": len(traces),
            "events": len(events),
            "requests": len(requests),
            "artifacts": len(artifacts),
            "decisions": len(decisions),
            "procedural_skill_bindings": len(procedural["bindings"]),
            "procedural_skill_versions": len(procedural["versions"]),
            "procedural_skill_consumption_events": len(procedural["consumption"]),
            "skill_summary_traces": len(procedural["summary_traces"]),
            "skill_summary_attempts": len(procedural["summary_attempts"]),
        },
        "run_ranges": [
            {
                "run_id": key,
                "events": len(value["events"]),
                "first_seq": value["events"][0]["seq"] if value["events"] else None,
                "last_seq": value["events"][-1]["seq"] if value["events"] else None,
                "requests": len(value["requests"]),
            }
            for key, value in traces.items()
        ],
        "missing_tables": sorted(required_tables - tables),
        "table_coverage": {
            table: "available" if table in tables else "not_recorded"
            for table in sorted(
                required_tables
                | {
                    "creative_intent_contracts",
                    "creative_intent_reviews",
                    "creative_intent_decisions",
                    "creative_intent_applications",
                    "creative_intent_ai_proposals",
                    "evolution_families",
                    "evolution_versions",
                    "evolution_states",
                    "evolution_experiences",
                    "evolution_episodes",
                    "evolution_episode_results",
                    "evolution_continuation_results",
                    "creative_v2_requirement_reports",
                    "creative_v2_trials",
                    "creative_v2_trial_results_v2",
                    "budget_attempt_receipts",
                    "budget_attempt_events",
                    "budget_grants",
                    "skill_cards",
                    "skill_card_versions",
                    "skill_card_events",
                    "skill_card_uses",
                }
            )
        },
        "missing_records": missing,
        "retrieval_complete": not missing and required_tables <= tables,
        "source_exclusions": _source_exclusions(decisions, measurements["dispatches"]),
        "training_export": {
            "eligible": False,
            "exported_samples": 0,
            "reason": "permissions_outcomes_and_behavior_coverage_require_separate_review",
        },
        "material_release_permission": "not_attested_local_evidence_only",
        "policy_assignments": [
            {
                "decision_id": row["id"],
                "policy_id": row.get("policy_id"),
                "policy_version": row.get("policy_version"),
            }
            for row in decisions
        ],
        "schema_versions": {
            "export": 4,
            "measurement": measurements["schema_version"],
            "decisions": sorted({row["schema_version"] for row in decisions}),
        },
        "cost_coverage": {
            key: measurements[key]
            for key in (
                "dispatch_count",
                "known_usage_dispatches",
                "unknown_usage_request_ids",
                "orphan_result_request_ids",
                "usage_complete",
                "cost_status",
            )
        },
    }
    return {
        "schema_version": 4,
        "exported_at": now(),
        "project": project,
        "task": task,
        "execution": state,
        "harness": {
            "events": trace["events"],
            "requests": trace["requests"],
            "replay_model_dispatches": 0,
            "initial_planning": planning,
        },
        "artifacts": artifacts,
        "skill": skill,
        "procedural_skills": procedural,
        "intent": intent,
        "recording": {
            "decisions": decisions,
            "traces": decision_traces,
            "measurements": measurements,
            "current_task_measurements": task_measurements,
            "budget": budget,
        },
        "manifest": manifest,
        "limits": [
            "author acceptance only from explicit recorded action",
            "mechanical coverage is not literary intention proof",
            "single case is not generalized improvement",
            "local records do not attest real transport, training consent or material release",
            "missing records and usage remain unknown; export never redispatches models",
            "response accumulates paged records in memory; it is not a streaming download",
            "linked method histories and task artifacts can include other runs from this project",
            "exported cost coverage is not current-task or lifetime-policy cost allocation",
            "skill binding is not consumed guidance; consumed guidance is not semantic compliance",
            "cross-task skill provenance does not assign its source learning cost to this task",
        ],
    }
