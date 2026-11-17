"""One bounded, evidence-bound graph repair through the ordinary production executor."""

from __future__ import annotations

import json

from ..content.hash import content_hash
from .harness import HarnessStore
from .models import now


def _seal(record, field):
    return {**record, field: content_hash(record)}


def _verify(record, field):
    if record.get(field) != content_hash({k: v for k, v in record.items() if k != field}):
        raise ValueError("修复依据完整性校验失败")


def _events(store, run_id):
    harness, cursor, result = HarnessStore(store), 0, []
    while page := harness.events(run_id, after_seq=cursor, limit=1000):
        result.extend(page)
        cursor = page[-1]["seq"]
    return result


def diagnose_repair(store, project, source_run_id):
    from .v2_service import v2_detail

    source = v2_detail(store, project, source_run_id)
    outcomes = (source.get("result") or {}).get("outcomes", {})
    failed = {key for key, value in outcomes.items() if value["status"] == "failed"}
    faults = []
    for event in _events(store, source_run_id):
        node_id, payload = event.get("node_id"), event["payload"]
        if node_id not in failed:
            continue
        if (
            event["type"] == "graph.node.failed"
            and payload.get("failure_domain") == "typed_contract"
        ):
            faults.append(
                {
                    "node_id": node_id,
                    "kind": "typed_contract",
                    "event_seq": event["seq"],
                    "validation_errors": payload.get("validation_errors", []),
                }
            )
        if event["type"] == "requirements/checked":
            for check in payload.get("checks", []):
                if (
                    check["status"] == "failed"
                    and check["blocking"]
                    and check["scope"] != "free_text"
                ):
                    faults.append(
                        {
                            "node_id": node_id,
                            "kind": "requirement_contract",
                            "check_key": check["key"],
                            "event_seq": event["seq"],
                        }
                    )
    covered = {row["node_id"] for row in faults}
    localizable = bool(failed) and failed <= covered
    return _seal(
        {
            "schema_version": 1,
            "project": project,
            "source_run_id": source_run_id,
            "task_id": source["task_id"],
            "revision": source["revision"],
            "graph_hash": source["graph_hash"],
            "result_hash": content_hash(source.get("result")),
            "status": "localized" if localizable else "needs_evidence",
            "failed_nodes": sorted(failed),
            "unlocalized_nodes": sorted(failed - covered),
            "faults": faults,
            "scope": "declared_contracts_only",
            "semantic_status": "unknown",
            "unknown_action": "expand_or_reexplore",
        },
        "diagnosis_hash",
    )


def propose_contract_repair(source, diagnosis):
    """Derive a local schema correction; no story text or model-inferred rule becomes a skill."""
    from .graph_delta import GraphEditIR, UpdateGuidance
    from .graph_v2 import PortSpec, compile_graph, narrative_role_registry

    _verify(diagnosis, "diagnosis_hash")
    if diagnosis["status"] != "localized" or diagnosis["graph_hash"] != source["graph_hash"]:
        raise ValueError("自动修复需要当前可定位合同失败")
    by_id = {node["id"]: node for node in source["graph"]["nodes"]}
    operations = []
    for node_id in diagnosis["failed_nodes"]:
        codes = sorted(
            {
                error["type"]
                for fault in diagnosis["faults"]
                if fault["node_id"] == node_id
                for error in fault.get("validation_errors", [])
            }
        )
        check_keys = sorted(
            {
                fault["check_key"]
                for fault in diagnosis["faults"]
                if fault["node_id"] == node_id and fault.get("check_key")
            }
        )
        if not codes and not check_keys:
            raise ValueError("缺少可执行错误细节，不能自动猜测修复指导")
        diagnosis_guidance = (
            "修复本节点上次输出的合同错误："
            + json.dumps(
                {"validation_error_types": codes, "failed_checks": check_keys}, ensure_ascii=False
            )
            + "。按本次output_schema与明确需求重新生成完整输出，保留原事实和保护项；"
            "不通过删要求、伪造检查结果或改变作者目标来规避错误。"
        )
        guidance = "\n".join(filter(None, [by_id[node_id].get("guidance", ""), diagnosis_guidance]))
        operations.append(UpdateGuidance(node_id=node_id, guidance=guidance))
    compiled = compile_graph(
        source["graph"], narrative_role_registry(), {"task": PortSpec(type_name="task_context")}
    )
    return GraphEditIR(
        expected_graph_hash=compiled.graph_hash,
        rationale="修正有正式失败依据的节点输出；拓扑、角色、权限和预算不变",
        operations=operations,
    ).model_dump(mode="json")


def _append(store, project, repair_id, phase, material):
    key = content_hash([project, repair_id, phase])
    with store.connection() as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS workflow_repair_events_v1("
            "id TEXT PRIMARY KEY,project TEXT NOT NULL,repair_id TEXT NOT NULL,"
            "phase TEXT NOT NULL,payload TEXT NOT NULL,UNIQUE(project,repair_id,phase))"
        )
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT payload FROM workflow_repair_events_v1 WHERE id=?", (key,)
        ).fetchone()
        if row:
            old = json.loads(row[0])
            _verify(old, "receipt_hash")
            if any(old.get(k) != v for k, v in material.items()):
                raise ValueError("同一修复事件不可覆盖")
            return old, False
        record = _seal(
            {
                "id": key,
                "project": project,
                "repair_id": repair_id,
                "phase": phase,
                **material,
                "created_at": now(),
            },
            "receipt_hash",
        )
        db.execute(
            "INSERT INTO workflow_repair_events_v1 VALUES(?,?,?,?,?)",
            (key, project, repair_id, phase, json.dumps(record, ensure_ascii=False)),
        )
    return record, True


def get_repair(store, project, repair_id):
    with store.connection() as db:
        rows = db.execute(
            "SELECT payload FROM workflow_repair_events_v1 WHERE project=? AND repair_id=? "
            "ORDER BY rowid",
            (project, repair_id),
        ).fetchall()
    if not rows:
        raise KeyError("修复记录不存在或不属于当前项目")
    records = [json.loads(row[0]) for row in rows]
    for record in records:
        _verify(record, "receipt_hash")
    return {
        "repair_id": repair_id,
        "events": records,
        **{record["phase"]: record for record in records},
    }


def validate_repair_contract(store, project, state):
    contract = state.get("repair_contract")
    if contract is None:
        return
    from .v2_service import _check_references, v2_detail

    _verify(contract, "contract_hash")
    source = v2_detail(store, project, contract["source_run_id"])
    if source["run"].get("cancel_requested"):
        raise ValueError("源运行已请求取消，停止修复继续派发和发布")
    task = store.get_task(source["task_id"])
    if (
        contract["project"] != project
        or state["task_id"] != contract["task_id"]
        or task.revision != contract["revision"]
        or state["revision"] != contract["revision"]
        or source["graph_hash"] != contract["source_graph_hash"]
        or content_hash(source.get("result")) != contract["source_result_hash"]
        or state["graph_hash"] != contract["target_graph_hash"]
        or content_hash(state["graph"]) != contract["target_graph_hash"]
        or source.get("collaboration", {}).get("policy_hash") != contract["policy_hash"]
        or source.get("collaboration", {}).get("policy", {}).get("allow_candidate_trial")
        is not True
        or state.get("prior_run_id") != contract["source_run_id"]
    ):
        raise ValueError("修复源版本、实际图、任务或试用许可已变化")
    _check_references(contract["root"], source["references"])


def prepare_repair_run(
    store,
    root,
    project,
    source_run_id,
    graph_edit=None,
    *,
    expected_diagnosis_hash,
    command_id,
):
    from .v2_service import prepare_v2_run, v2_detail
    from .v2_skills import compile_business_edit

    if not command_id or not isinstance(command_id, str) or len(command_id) > 200:
        raise ValueError("修复命令需要稳定身份")
    source = v2_detail(store, project, source_run_id)
    diagnosis = diagnose_repair(store, project, source_run_id)
    if diagnosis["diagnosis_hash"] != expected_diagnosis_hash:
        raise ValueError("修复诊断已变化")
    if diagnosis["status"] != "localized":
        raise ValueError("未定位或未知语义影响，须扩大证据范围或重新探索")
    if graph_edit is None:
        graph_edit = propose_contract_repair(source, diagnosis)
    if source.get("collaboration", {}).get("policy", {}).get("allow_candidate_trial") is not True:
        raise ValueError("当前约定未允许候选试用，不能自动执行修复图")
    compiled = compile_business_edit(source["graph"], graph_edit)
    applied = compiled["applied"]
    if not set(diagnosis["failed_nodes"]) <= set(applied["affected_nodes"]):
        raise ValueError("修改范围没有覆盖已定位失败")
    mandatory = {
        node["id"]
        for node in applied["graph"]["nodes"]
        if node["operation"] in {"validate_decisions", "coverage_audit"}
    }
    repair_id = content_hash([project, source_run_id, command_id])
    contract = _seal(
        {
            "schema_version": 1,
            "repair_id": repair_id,
            "project": project,
            "root": str(root),
            "task_id": source["task_id"],
            "revision": source["revision"],
            "source_run_id": source_run_id,
            "source_graph_hash": source["graph_hash"],
            "source_result_hash": diagnosis["result_hash"],
            "target_graph_hash": content_hash(applied["graph"]),
            "diagnosis": diagnosis,
            "graph_compilation": compiled,
            "invalidated_nodes": sorted(set(applied["affected_nodes"]) | mandatory),
            "policy_hash": source["collaboration"]["policy_hash"],
            "semantic_status": "unknown",
            "author_acceptance": "unknown",
        },
        "contract_hash",
    )
    _claim, fresh = _append(
        store,
        project,
        repair_id,
        "prepare_claim",
        {
            "task_id": source["task_id"],
            "request_hash": content_hash([diagnosis, graph_edit, str(root)]),
        },
    )
    if not fresh:
        return get_repair(store, project, repair_id)
    task = store.get_task(source["task_id"])
    child = prepare_v2_run(
        store,
        root,
        task,
        applied["graph"],
        prior_run_id=source_run_id,
        machine_requirements=source["requirements"]["machine"],
        collaboration_policy=source["collaboration"]["policy"],
        narrative_project_id=source.get("narrative_project_id"),
        narrative_scene_id=source.get("narrative_scene_id"),
        narrative_preview_id=source.get("narrative_preview_id"),
        narrative_transition_id=source.get("narrative_transition_id"),
        repair_contract=contract,
        apply_project_skills=False,
        summarize_skill=source.get("summarize_skill") is True,
    )
    if child.get("repair_contract") != contract:
        raise ValueError("修复合同未与实际运行原子保存")
    validate_repair_contract(store, project, child)
    _append(
        store,
        project,
        repair_id,
        "prepared",
        {
            "task_id": task.id,
            "run_id": child["run_id"],
            "contract": contract,
        },
    )
    return get_repair(store, project, repair_id)


def maybe_repair_execution(store, root, project, result, *, emit=None):
    """Try one authorized contract repair; never recursively repair or replay unknown delivery."""
    from .skill_cards import SkillCards
    from .v2_service import v2_detail

    if result.get("repair_contract") or result.get("run", {}).get("status") not in {
        "failed",
        "completed_with_skips",
    }:
        return result
    source_id = result["run_id"]
    source = v2_detail(store, project, source_id)
    if source["run"].get("cancel_requested"):
        return result
    if source.get("collaboration", {}).get("policy", {}).get("allow_candidate_trial") is not True:
        return result
    diagnosis = diagnose_repair(store, project, source_id)
    if diagnosis["status"] != "localized":
        return result
    if any(
        event["type"] == "model/result"
        and event["payload"].get("status") == "failed"
        and event.get("node_id") in diagnosis["failed_nodes"]
        for event in _events(store, source_id)
    ):
        return result
    try:
        SkillCards(store).assert_bindings(project, source.get("skill_card_bindings", []))
        prepared = prepare_repair_run(
            store,
            root,
            project,
            source_id,
            expected_diagnosis_hash=diagnosis["diagnosis_hash"],
            command_id="automatic:" + source_id,
        )
        if "prepared" not in prepared:
            return {
                **result,
                "automatic_repair": {
                    "source_run_id": source_id,
                    "repair_id": prepared["repair_id"],
                    "status": "prepare_claimed_unknown",
                    "receipt": prepared,
                },
            }
        source_run = store.get_run(source_id)
        child_run = store.get_run(prepared["prepared"]["run_id"])
        if source_run.job_id and child_run.status in {"queued", "running", "waiting_user"}:
            if child_run.job_id not in {None, source_run.job_id}:
                raise ValueError("修复运行已经关联其他工作，不能改写归属")
            if child_run.job_id is None:
                store.update_run(child_run.id, job_id=source_run.job_id)
        repaired = execute_repair_run(store, root, project, prepared["repair_id"], emit=emit)
    except ValueError as error:
        if emit:
            emit(
                "repair/declined",
                {
                    "source_run_id": source_id,
                    "reason": "repair_admission_failed",
                    "error": str(error),
                },
            )
        return result
    child = v2_detail(store, project, prepared["prepared"]["run_id"])
    return {
        **child,
        "automatic_repair": {
            "source_run_id": source_id,
            "repair_id": repaired["repair_id"],
            "receipt": repaired,
        },
    }


def execute_repair_run(store, root, project, repair_id, *, provider=None, emit=None):
    from .v2_service import execute_v2_run, v2_detail

    record = get_repair(store, project, repair_id)
    if "outcome" in record:
        return record
    if "prepared" not in record:
        return {**record, "status": "prepare_claimed_unknown", "new_dispatch": False}
    run_id = record["prepared"]["run_id"]
    state = v2_detail(store, project, run_id)
    validate_repair_contract(store, project, state)
    _claim, fresh = _append(store, project, repair_id, "execution_claim", {"run_id": run_id})
    if not fresh and state["run"]["status"] not in {
        "completed",
        "completed_with_skips",
        "cancelled",
        "failed",
    }:
        return {**record, "status": "execution_claimed_unknown", "new_dispatch": False}
    child = (
        execute_v2_run(store, root, project, run_id, provider=provider, emit=emit)
        if fresh
        else state
    )
    if child["run"]["status"] == "waiting_user":
        return {**get_repair(store, project, repair_id), "status": "waiting_user", "run_id": run_id}
    result = child.get("result") or {}
    checks = [event for event in _events(store, run_id) if event["type"] == "requirements/checked"]
    hard_issues = [
        issue
        for issue in result.get("outputs", {}).get("report", {}).get("issues", [])
        if issue.get("severity") == "error"
    ]
    q0 = "unknown"
    if hard_issues or any(not event["payload"]["can_continue"] for event in checks):
        q0 = "fail"
    elif result.get("status") == "completed" and checks:
        q0 = "pass"
    _append(
        store,
        project,
        repair_id,
        "outcome",
        {
            "run_id": run_id,
            "source_run_id": state["repair_contract"]["source_run_id"],
            "graph_edit": state["repair_contract"]["graph_compilation"]["edit"],
            "affected_nodes": state["repair_contract"]["invalidated_nodes"],
            "method_candidate_scope": "source_task_only_until_parameterized",
            "status": child["run"]["status"],
            "checked_event_seqs": [event["seq"] for event in checks],
            "outcomes": {key: value["status"] for key, value in result.get("outcomes", {}).items()},
            "actual_dispatch_ids": [row["id"] for row in HarnessStore(store).requests(run_id)],
            "Q0": q0,
            "Q0_scope": "recorded_machine_checks_and_audit",
            "Q1": "unknown",
            "Q2": "unknown",
            "author_acceptance": "unknown",
        },
    )
    return get_repair(store, project, repair_id)
