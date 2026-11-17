"""Run the original workflow against its stored, real IR with updated compilers.

This is an explicit deterministic execution mode, never a provider replay or a
new generation claim. The same typed nodes and official checkpointed driver run;
canonical requirements, decision, coverage and project audits are recomputed.
"""

from __future__ import annotations

import json
from typing import Any

from ..content.hash import content_hash
from ..content.models import DialogueTree, Quest
from .budget_policy import BudgetLedger
from .collaboration_v2 import CollaborationStore
from .decision_contract import validate_decisions
from .graph_driver import execute_persistent_graph
from .graph_v2 import Handler, NodeSpecV2, PortSpec, compile_graph, narrative_role_registry
from .harness import HarnessStore
from .mechanic_ir_v2 import materialize_quest_draft
from .models import Artifact
from .narrative_audit_v2 import audit_narrative_bound
from .narrative_recompile_v2 import recompile_bound_localization
from .requirements_v2 import check_role_requirements, verify_requirement_binding


def execute_localization_recompile(
    store,
    root,
    project,
    source_run_id,
    *,
    expected_source_graph_hash,
    expected_source_bound_hash,
    attempt_reason=None,
    emit=None,
) -> dict[str, Any]:
    """Append a distinct trial/evaluation without altering the old failed artifact.

    Zero model dispatches. A known failure needs a new diagnosis reason; an
    unknown claim is only returned for reconciliation, never silently replayed.
    """
    from .v2_service import _check_references, prepare_v2_run, v2_detail
    from .v2_skills import check_run_authorization, evaluate_skill_run

    source = v2_detail(store, project, source_run_id)
    result = source.get("result") or {}
    bound = (result.get("outputs") or {}).get("bound")
    if not bound or not result.get("artifacts"):
        raise ValueError("确定性重编需要完整真实产物，不能从空运行编造IR")
    # v2_detail.graph_hash is the legacy raw-spec digest. The executable
    # identity includes registered role contracts and lives in result.graph_hash.
    if (
        result.get("graph_hash") != expected_source_graph_hash
        or content_hash(bound) != expected_source_bound_hash
    ):
        raise ValueError("重编来源graph／bound hash已变化")
    task = store.get_task(source["task_id"])
    if source["run"]["revision"] != task.revision:
        raise ValueError("源产物任务版本已过期")
    _check_references(root, source["references"])
    check_run_authorization(store, project, source)
    plan_artifact = next(
        row for row in result["artifacts"].values() if row["type_name"] == "event_plan"
    )
    plan = plan_artifact["value"]
    recompilation = recompile_bound_localization(plan, bound)
    for key in ("quest_logic_unchanged", "dialogue_content_and_flow_unchanged"):
        if recompilation["invariants"].get(key) is not True:
            raise ValueError("重编不能改变剧情／机制／路由／后果")
    version_id = source.get("skill_version_id")
    if not version_id:
        raise ValueError("此技术trial必须绑定实际来源方法版本")
    requests = HarnessStore(store).requests(source_run_id)
    source_hash = content_hash(result)
    fingerprint = content_hash(
        {
            "source_run_id": source_run_id,
            "source_hash": source_hash,
            "recompilation_hash": recompilation["recompilation_hash"],
        }
    )
    operation_key = "stored-ir-localization:" + fingerprint
    ledger = BudgetLedger(store)
    previous = ledger.execution_state(operation_key)
    if previous["status"] != "new" and (
        previous["status"] != "completed_failed" or not attempt_reason
    ):
        return {"status": previous["status"], "attempt_state": previous, "new_dispatch": False}
    claim = ledger.claim_execution(
        operation_key,
        content_hash([project, task.id, "stored_ir_recompilation"]),
        fingerprint,
        reason=attempt_reason
        or "Recompile stored real IR with stable localization IDs and actual strings",
    )
    if not claim.get("new_dispatch"):
        return {
            "status": claim["status"],
            "attempt_state": ledger.execution_state(operation_key),
            "new_dispatch": False,
        }
    events = HarnessStore(store)
    run_id = None
    try:
        prepared = prepare_v2_run(
            store,
            root,
            task,
            source["graph"],
            skill_version_id=version_id,
            skill_authorization=source.get("skill_authorization"),
            machine_requirements=source["requirements"]["machine"],
            collaboration_policy=source["collaboration"]["policy"],
        )
        run_id = prepared["run_id"]
        store.update_run(run_id, status="running")
        state = v2_detail(store, project, run_id)
        requirements = verify_requirement_binding(state["requirements"], task)
        compiled = compile_graph(
            state["graph"], narrative_role_registry(), {"task": PortSpec(type_name="task_context")}
        )
        if compiled.graph_hash != expected_source_graph_hash:
            raise ValueError("重编运行必须保留原候选的实际图结构／指导合同")
        provenance = {
            "execution_mode": "stored_ir_recompilation_no_model_dispatch",
            "source_run_id": source_run_id,
            "source_result_hash": source_hash,
            "source_requests": [
                {
                    "id": row["id"],
                    "request_hash": row["request_hash"],
                    "budget_id": row["budget_id"],
                    "attempt": row["attempt"],
                }
                for row in requests
            ],
            "source_artifacts": {
                key: row["content_hash"] for key, row in result["artifacts"].items()
            },
            "recompilation": recompilation,
            "execution_attempt_id": claim["id"],
            "model_dispatches": 0,
            "author_acceptance": "unknown",
        }
        with store.connection() as db:
            saved = json.loads(
                db.execute(
                    "SELECT payload FROM creative_v2_runs WHERE run_id=?", (run_id,)
                ).fetchone()[0]
            )
            saved["recompilation"] = provenance
            db.execute(
                "UPDATE creative_v2_runs SET payload=? WHERE run_id=?",
                (json.dumps(saved, ensure_ascii=False), run_id),
            )
        events.append(run_id, "recompilation/source_bound", provenance)
        new_bound = recompilation["bound"]
        original_draft = Quest.model_validate(new_bound["quest"]).metadata["quest_draft_v2"][
            "source"
        ]
        draft = materialize_quest_draft(original_draft, quest_id=new_bound["quest"]["id"])
        context = {
            "task": {
                "task_id": task.id,
                "brief": task.request.brief,
                "protected": task.request.protected,
                "base_hash": task.base_hash,
                "entities": list(task.base_bundle.get("entities", {}).values()),
                "machine_requirements": requirements.machine.model_dump(mode="json"),
            }
        }

        def cancelled():
            _check_references(root, source["references"])
            check_run_authorization(store, project, state)
            return (
                store.get_task(task.id).revision != task.revision
                or store.get_run(run_id).cancel_requested
                or content_hash(v2_detail(store, project, source_run_id).get("result") or {})
                != source_hash
            )

        def source_node(node, inputs):
            if node.operation == "plan_events":
                return {"plan": plan}
            if content_hash(inputs["plan"]) != content_hash(plan):
                raise ValueError("重编节点必须消费原真实plan")
            if node.operation == "draft_quest":
                return {"quest": draft.model_dump(mode="json")}
            if node.operation == "logic_contract":
                if content_hash(inputs["quest"]) != content_hash(draft.model_dump(mode="json")):
                    raise ValueError("机制重编必须消费本次正式重编草稿")
                return {"logic": new_bound["quest"], "decisions": recompilation["decisions"]}
            if node.operation == "dialogue_constrained":
                if inputs["contract"].get("status") != "validated":
                    raise ValueError("对白重编仍需要实际已验证决策合同")
                return {"dialogue": new_bound["tree"], "choices": recompilation["choices"]}
            raise ValueError("来源图含未支持的存储IR角色；不回退模型或模板")

        def validate(node, inputs):
            return {
                "contract": validate_decisions(inputs["plan"], inputs["logic"], inputs["decisions"])
            }

        def bind(node, inputs):
            quest = Quest.model_validate(inputs["logic"])
            tree = DialogueTree.model_validate(inputs["dialogue"])
            if tree.quest_id != quest.id:
                raise ValueError("重编后绑定对象不一致")
            return {
                "bound": {
                    "quest": quest.model_dump(mode="json"),
                    "tree": tree.model_dump(mode="json"),
                }
            }

        def audit(node, inputs):
            return {
                "report": audit_narrative_bound(
                    root,
                    task,
                    plan=inputs["plan"],
                    bound=inputs["bound"],
                    decisions=inputs.get("decisions"),
                    choices=inputs.get("choices"),
                    require_distinct=requirements.machine.distinct_outcomes is True,
                )
            }

        base_handlers = {
            "plan_events": source_node,
            "draft_quest": source_node,
            "logic_contract": source_node,
            "dialogue_constrained": source_node,
            "validate_decisions": validate,
            "bind_dialogue": bind,
            "coverage_audit": audit,
        }

        def checked(handler: Handler) -> Handler:
            def execute(node: NodeSpecV2, inputs: dict[str, Any]) -> dict[str, Any]:
                output = handler(node, inputs)
                report = check_role_requirements(
                    requirements, node.operation, output, inputs=inputs
                )
                events.append(
                    run_id, "requirements/checked", report.model_dump(mode="json"), node_id=node.id
                )
                with store.connection() as db:
                    db.execute(
                        "INSERT OR REPLACE INTO creative_v2_requirement_reports VALUES(?,?,?)",
                        (run_id, node.id, report.model_dump_json()),
                    )
                if not report.can_continue:
                    raise ValueError("正式需求检查拒绝重编产物：" + report.model_dump_json())
                return output

            return execute

        def notify(kind, payload, **kwargs):
            events.append(run_id, kind, payload, node_id=kwargs.get("node_id"))
            if emit:
                emit(kind, payload)

        collaboration = CollaborationStore(store)
        execution = execute_persistent_graph(
            compiled,
            context,
            {key: checked(value) for key, value in base_handlers.items()},
            checkpoint_path=store.path.parent / "graph-v2-checkpoints.sqlite3",
            thread_id=run_id,
            emit=notify,
            cancelled=cancelled,
            context_identity={
                "mode": provenance["execution_mode"],
                "source_result_hash": source_hash,
                "recompilation_hash": recompilation["recompilation_hash"],
                "requirements": requirements.contract_hash,
            },
            before_node=lambda node, current: collaboration.before_node(
                project, run_id, node, current, graph_hash=compiled.graph_hash
            ),
            consume_resume=lambda gate, decision: collaboration.consume_resume(
                project, run_id, gate, decision
            ),
            resume=collaboration.resume_value(project, run_id),
        )
        with store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            saved = json.loads(
                db.execute(
                    "SELECT payload FROM creative_v2_runs WHERE run_id=?", (run_id,)
                ).fetchone()[0]
            )
            saved["result"] = execution.model_dump(mode="json")
            if "bound" in execution.outputs and not cancelled():
                payload = execution.outputs["bound"]
                artifact = Artifact(
                    task_id=task.id,
                    revision=task.revision,
                    run_id=run_id,
                    node_id=compiled.spec.outputs["bound"].node_id,
                    kind="bound_dialogue",
                    payload=payload,
                    input_hash=fingerprint,
                    content_hash=content_hash(payload),
                )
                db.execute(
                    "INSERT INTO artifacts VALUES(?,?,?)",
                    (artifact.id, task.id, artifact.model_dump_json()),
                )
                saved["feedback_source"] = {
                    "artifact_id": artifact.id,
                    "artifact_hash": artifact.content_hash,
                    "kind": artifact.kind,
                }
            db.execute(
                "UPDATE creative_v2_runs SET payload=? WHERE run_id=?",
                (json.dumps(saved, ensure_ascii=False), run_id),
            )
        store.update_run(run_id, status=execution.status)
        evaluation = None
        if execution.status != "waiting_user":
            evaluation = evaluate_skill_run(store, project, run_id, version_id)
        receipt = {
            "status": execution.status,
            "run_id": run_id,
            "execution_mode": provenance["execution_mode"],
            "recompilation_hash": recompilation["recompilation_hash"],
            "model_dispatches": 0,
            "evaluation": evaluation,
            "author_acceptance": "unknown",
        }
        if execution.status != "waiting_user":
            ledger.finish_execution(
                claim["id"],
                status="completed"
                if evaluation and evaluation["technical_status"] == "passed"
                else "completed_failed",
                evidence=receipt,
            )
        events.append(run_id, "recompilation/result", receipt)
        return receipt
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        if run_id is not None:
            store.update_run(run_id, status="failed", error=error)
            events.append(run_id, "recompilation/failed", {"error": error})
        ledger.finish_execution(
            claim["id"], status="completed_failed", evidence={"error": error, "run_id": run_id}
        )
        raise
