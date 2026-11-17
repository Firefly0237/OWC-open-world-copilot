"""Permission-bounded skill maintenance decisions and conservative offline transitions."""

from __future__ import annotations

import json
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from ..content.hash import content_hash
from .execution_records import (
    DecisionReceipt,
    ExecutionRecords,
    SourceAxes,
    _decode,
    summarize_dispatches,
)
from .harness import HarnessStore
from .skill_cards import SkillSelection, _read

_ACTIONS = ("retain", "summarize", "update", "no_change")
_POLICY_ID = "skill_maintenance_rules"
_OUTCOME_EVENT = "skill/maintenance_outcome"
_PRODUCTION_ROLES = {
    "plan_events",
    "draft_quest",
    "draft_logic",
    "logic_contract",
    "draft_dialogue",
    "dialogue_constrained",
}


class SkillPolicyChoice(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    action_id: Literal["retain", "summarize", "update", "no_change"]
    reason: str = Field(min_length=1, max_length=500)
    selection_kind: Literal["deterministic", "unknown"] = "deterministic"
    selection_probability: None = None


class SkillMaintenancePolicy(Protocol):
    id: str
    version: str

    def select(
        self, observation: dict, feasible_actions: list[dict], budget: dict
    ) -> SkillPolicyChoice: ...


class RuleMaintenancePolicy:
    id = _POLICY_ID
    version = "1"

    def select(self, observation, feasible_actions, budget):
        allowed = {action["id"] for action in feasible_actions}
        grounds = bool(observation["repair_contract_hash"] or observation["method_feedback_refs"])
        if "update" in allowed:
            return SkillPolicyChoice(
                action_id="update", reason="已有明确目标及修复或方法反馈，生成更新候选"
            )
        if observation["skill_bindings"] and not grounds and "retain" in allowed:
            return SkillPolicyChoice(
                action_id="retain", reason="普通复用未提供新的维护依据，保留定义且不再总结"
            )
        if "summarize" in allowed:
            return SkillPolicyChoice(
                action_id="summarize", reason="作者已开启总结，从实际制作结果提炼候选"
            )
        action = "retain" if "retain" in allowed else "no_change"
        return SkillPolicyChoice(action_id=action, reason="当前不满足新增维护调用条件")


def _target(store, project, raw):
    if raw is None:
        return None
    selected = SkillSelection.model_validate(raw)
    with store.connection() as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"skill_cards", "skill_card_versions", "skill_card_events"} <= tables:
            raise ValueError("更新目标缺少正式Skill记录")
        head = db.execute(
            "SELECT payload FROM skill_cards WHERE project=? AND id=?", (project, selected.card_id)
        ).fetchone()
        version = db.execute(
            "SELECT payload FROM skill_card_versions WHERE project=? AND id=?",
            (project, selected.version_id),
        ).fetchone()
        if head is None or version is None:
            raise ValueError("更新目标不属于当前工作区")
        head, version = _read(head[0], "card_hash"), _read(version[0], "version_hash")
        if (
            not head["enabled"]
            or version["card_id"] != selected.card_id
            or version["version_hash"] != selected.version_hash
        ):
            raise ValueError("更新目标已停用或版本不一致")
        state = "candidate"
        for row in db.execute(
            "SELECT payload FROM skill_card_events WHERE project=? AND card_id=? ORDER BY seq",
            (project, selected.card_id),
        ):
            event = _read(row[0], "event_hash")
            if event["payload"].get("version_id") == selected.version_id and event["kind"] in {
                "accepted",
                "rejected",
                "retired",
            }:
                state = event["kind"]
        if state in {"rejected", "retired"}:
            raise ValueError("更新目标已撤回")
    return selected.model_dump(mode="json")


def _observation(store, project, result, update_target, feedback_refs):
    task = store.get_task(result["task_id"])
    run = store.get_run(result["run_id"])
    if task.project != project or run.task_id != task.id or run.revision != task.revision:
        raise ValueError("维护决策与当前任务版本不一致")
    with store.connection() as db:
        row = db.execute(
            "SELECT payload FROM creative_v2_runs WHERE run_id=? AND project=?", (run.id, project)
        ).fetchone()
    if row is None:
        raise ValueError("维护决策缺少冻结运行")
    state = json.loads(row[0])
    if (
        state["task_id"] != task.id
        or state["revision"] != run.revision
        or (
            content_hash(state["graph"]) != state["graph_hash"]
            or content_hash(state.get("result")) != content_hash(result.get("result"))
        )
    ):
        raise ValueError("维护决策来源已经改变")
    if any(not isinstance(value, str) or not value.strip() for value in feedback_refs):
        raise ValueError("维护反馈必须提供明确记录引用")
    refs = sorted(set(feedback_refs))
    allowed_refs = set()
    for feedback in task.feedback:
        if (
            feedback.get("scope") == "workflow"
            or feedback.get("target_operation") == "workflow_spec"
        ):
            digest = content_hash(feedback)
            allowed_refs.update({digest, "feedback:" + digest})
            if isinstance(feedback.get("id"), str):
                allowed_refs.add(feedback["id"])
    if set(refs) - allowed_refs:
        raise ValueError("维护反馈引用不是当前任务中明确记录的工作流反馈")
    bindings = [
        {key: binding[key] for key in ("card_id", "version_id", "version_hash", "selection")}
        for binding in state.get("skill_card_bindings", [])
    ]
    outcomes = (state.get("result") or {}).get("outcomes", {})
    production_nodes = {
        node["id"] for node in state["graph"]["nodes"] if node["operation"] in _PRODUCTION_ROLES
    }
    repair = state.get("repair_contract")
    if repair and (
        repair.get("contract_hash")
        != content_hash({key: value for key, value in repair.items() if key != "contract_hash"})
        or repair.get("target_graph_hash") != state["graph_hash"]
        or repair.get("task_id") != task.id
        or repair.get("revision") != task.revision
    ):
        raise ValueError("维护决策的修复合同与实际图／任务不一致")
    observation = {
        "decision_kind": "skill_maintenance",
        "state_schema_version": 1,
        "action_schema_version": 1,
        "source_run_id": run.id,
        "task_id": task.id,
        "task_revision": task.revision,
        "source_graph_hash": state["graph_hash"],
        "source_result_hash": content_hash(state.get("result")),
        "source_status": run.status,
        "cancel_requested": run.cancel_requested,
        "summary_requested": state.get("summarize_skill") is True,
        "has_production_outputs": any(
            value["status"] in {"succeeded", "reused"} and value.get("outputs")
            for node_id, value in outcomes.items()
            if node_id in production_nodes
        ),
        "skill_bindings": bindings,
        "binding_is_consumption": False,
        "repair_contract_hash": (repair or {}).get("contract_hash"),
        "method_feedback_refs": refs,
        "feedback_actor_origin": "unknown",
        "update_target": _target(store, project, update_target),
        "source_policy_hash": (result.get("collaboration") or {}).get("policy_hash"),
        "author_quality": "unknown",
        "semantic_status": "unknown",
    }
    return task, observation


def _actions(observation, budget):
    for key in ("remaining_dispatches", "remaining_tokens"):
        value = budget.get(key)
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError("维护预算必须是非负整数或未知")
    paid_reasons = []
    for blocked, reason in (
        (not observation["summary_requested"], "author_summary_not_requested"),
        (observation["cancel_requested"], "source_cancelled"),
        (
            observation["source_status"] not in {"completed", "completed_with_skips"},
            "source_not_completed",
        ),
        (not observation["has_production_outputs"], "no_observed_production_output"),
        (budget.get("remaining_dispatches") == 0, "dispatch_budget_exhausted"),
        (budget.get("remaining_tokens") is not None, "unknown_tokens_cannot_satisfy_explicit_cap"),
    ):
        if blocked:
            paid_reasons.append(reason)
    result = []
    for action in _ACTIONS:
        reasons = list(paid_reasons) if action in {"summarize", "update"} else []
        if action == "retain" and (
            not observation["skill_bindings"] or observation["cancel_requested"]
        ):
            reasons.append("no_active_source_selection_or_cancelled")
        if action == "update":
            if not observation["update_target"]:
                reasons.append("explicit_update_target_required")
            if not (observation["repair_contract_hash"] or observation["method_feedback_refs"]):
                reasons.append("no_repair_or_method_feedback_record")
        result.append(
            {
                "id": action,
                "action_schema_version": 1,
                "eligible": not reasons,
                "rejection_reasons": reasons,
                "candidate_only": action in {"summarize", "update"},
                "author_adoption_or_permission_change": False,
                "dispatch_reservation": 1 if action in {"summarize", "update"} else 0,
                "estimated_tokens": None if action in {"summarize", "update"} else 0,
            }
        )
    return result


def record_maintenance_decision(
    store, project, result, *, update_target=None, feedback_refs=(), budget=None, policy=None
):
    """Select and record an allowed action; the existing execution entry points retain authority."""
    budget = dict(budget or {})
    task, observation = _observation(store, project, result, update_target, feedback_refs)
    actions = _actions(observation, budget)
    policy = policy or RuleMaintenancePolicy()
    identity = content_hash(
        [
            "skill-maintenance-v1",
            project,
            task.id,
            observation,
            actions,
            budget,
            policy.id,
            policy.version,
        ]
    )
    records = ExecutionRecords(store)
    old = next((row for row in records.for_task(project, task.id) if row["id"] == identity), None)
    if old is not None:
        records.bind_run(project, identity, observation["source_run_id"])
        return {
            **old,
            "run_ids": sorted(set(old.get("run_ids", [])) | {observation["source_run_id"]}),
        }
    choice = SkillPolicyChoice.model_validate(
        policy.select(
            json.loads(json.dumps(observation)),
            [dict(action) for action in actions if action["eligible"]],
            dict(budget),
        )
    )
    if choice.action_id not in {action["id"] for action in actions if action["eligible"]}:
        raise ValueError("维护策略选择了当前许可外的动作")
    receipt = DecisionReceipt(
        id=identity,
        project=project,
        task_id=task.id,
        task_revision=task.revision,
        input_hash=content_hash(task.request),
        policy_id=policy.id,
        policy_version=policy.version,
        observation=observation,
        candidates=actions,
        selected_action_id=choice.action_id,
        reason=choice.reason,
        budget=budget,
        selection_kind=choice.selection_kind,
        selection_probability=None,
        source=SourceAxes(task_origin=task.origin_kind),
    )
    try:
        record = records.append_decision(receipt)
    except ValueError:
        current = store.get_task(task.id)
        if (current.project, current.revision, content_hash(current.request)) != (
            project,
            task.revision,
            content_hash(task.request),
        ):
            raise
        concurrent = next(
            (row for row in records.for_task(project, task.id) if row["id"] == identity), None
        )
        if concurrent is None:
            raise
        record = concurrent
    records.bind_run(project, identity, observation["source_run_id"])
    return {**record, "run_ids": [observation["source_run_id"]]}


def record_maintenance_outcome(
    store,
    project,
    decision_id,
    *,
    status,
    summary_run_id=None,
    new_dispatch=None,
    technical_status="unknown",
):
    """Attest the host branch taken, without inventing a preference or a scalar reward."""
    if (
        status not in {"not_dispatched", "completed", "failed", "unknown"}
        or technical_status
        not in {
            "completed",
            "failed",
            "unknown",
        }
        or (new_dispatch is not None and type(new_dispatch) is not bool)
    ):
        raise ValueError("维护结果状态不受支持")
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT payload FROM creative_method_decisions WHERE project=? AND id=?",
            (project, decision_id),
        ).fetchone()
        if row is None:
            raise KeyError("维护决策不存在")
        decision = _decode(row[0])
        if decision["observation"].get("decision_kind") != "skill_maintenance":
            raise ValueError("记录不是Skill维护决策")
        if status == "not_dispatched" and (new_dispatch is not False or summary_run_id is not None):
            raise ValueError("未派发结果不能携带新调用或总结运行")
        if decision["selected_action_id"] in {"retain", "no_change"} and status != "not_dispatched":
            raise ValueError("保留或无变更动作不能声称执行了总结")
        if summary_run_id:
            summary = db.execute(
                "SELECT task_id,payload FROM runs WHERE id=?", (summary_run_id,)
            ).fetchone()
            if (
                summary is None
                or summary[0] != decision["task_id"]
                or (json.loads(summary[1])["revision"] != decision["task_revision"])
            ):
                raise ValueError("总结运行不属于当前维护任务版本")
        payload = {
            "decision_id": decision_id,
            "action_id": decision["selected_action_id"],
            "source_run_id": decision["observation"]["source_run_id"],
            "status": status,
            "summary_run_id": summary_run_id,
            "new_dispatch": new_dispatch,
            "technical_status": technical_status,
            "author_quality": "unknown",
            "reward": None,
        }
        previous = db.execute(
            "SELECT payload FROM creative_harness_events WHERE run_id=? AND event_type=? "
            "AND json_extract(payload,'$.payload.decision_id')=? ORDER BY seq",
            (payload["source_run_id"], _OUTCOME_EVENT, decision_id),
        ).fetchall()
        for old in previous:
            event = _read(old[0], "event_hash")
            if event["payload"] == payload:
                return event
            prior = event["payload"]
            if (
                prior["status"] == status == "completed"
                and prior["summary_run_id"] == summary_run_id
                and summary_run_id is not None
                and prior["new_dispatch"] is True
                and new_dispatch is False
            ):
                return event
            if (
                prior["status"] != "unknown"
                or prior["summary_run_id"] not in {None, summary_run_id}
                or prior["new_dispatch"] not in {None, new_dispatch}
            ):
                raise ValueError("已确认维护结果不可覆盖")
        return HarnessStore._event(db, payload["source_run_id"], _OUTCOME_EVENT, payload)


def _traces(evidence):
    result = {}
    execution = evidence.get("execution", {})
    if execution.get("run_id"):
        result[execution["run_id"]] = {
            "run": execution.get("run", {}),
            "events": evidence.get("harness", {}).get("events", []),
            "requests": evidence.get("harness", {}).get("requests", []),
        }
    for trace in evidence.get("recording", {}).get("traces", []) + evidence.get(
        "procedural_skills", {}
    ).get("summary_traces", []):
        result[trace["run"]["id"]] = trace
    return result


def isolate_maintenance_cost(evidence, decision):
    """Charge only an explicitly linked fresh summary, never production or prior learning."""
    traces = _traces(evidence)
    source_id = decision["observation"]["source_run_id"]
    outcomes = {
        event["event_hash"]: event
        for trace in traces.values()
        for event in trace["events"]
        if event["type"] == _OUTCOME_EVENT and event["payload"].get("decision_id") == decision["id"]
    }
    latest = max(outcomes.values(), key=lambda event: event.get("seq", 0), default=None)
    result = {
        "scope": "this_maintenance_action_only",
        "dispatch_count": None,
        "total_tokens": None,
        "known_totals": None,
        "cost_usd": None,
        "cost_status": "not_attested",
        "unknown_usage_request_ids": [],
        "charged_request_ids": [],
        "excluded_source_run_id": source_id,
        "source_history_summary_run_ids": [],
        "host_duration_seconds": None,
        "author_interaction_seconds": None,
    }
    if latest is None:
        return result
    outcome = latest["payload"]
    result["outcome"] = outcome
    summary_id = outcome["summary_run_id"]
    if outcome["new_dispatch"] is False:
        return {
            **result,
            "dispatch_count": 0,
            "total_tokens": 0,
            "cost_status": "no_new_model_dispatch_attested",
            "source_history_summary_run_ids": [summary_id] if summary_id else [],
        }
    if outcome["new_dispatch"] is not True or summary_id not in decision.get("run_ids", []):
        return result
    trace = traces.get(summary_id)
    if trace is None:
        return result
    competing = [
        row["id"]
        for row in evidence.get("recording", {}).get("decisions", [])
        if row["id"] != decision["id"]
        and summary_id in row.get("run_ids", [])
        and row.get("observation", {}).get("decision_kind") == "skill_maintenance"
    ]
    if competing:
        return {
            **result,
            "cost_status": "ambiguous_decision_attribution",
            "conflicting_decision_ids": competing,
        }
    requests = trace["requests"]
    if not requests or any(
        request["request"].get("role_id") != "skill_summary"
        or (
            request["context_manifest"].get("source_run_id") != source_id
            or request["context_manifest"].get("purpose") != "procedural_skill_candidate"
        )
        for request in requests
    ):
        return result
    measured = summarize_dispatches(requests, trace["events"])
    return {
        **result,
        **{
            key: measured[key]
            for key in (
                "dispatch_count",
                "total_tokens",
                "known_totals",
                "cost_usd",
                "cost_status",
                "unknown_usage_request_ids",
                "usage_complete",
            )
        },
        "charged_request_ids": [request["id"] for request in requests],
    }


def offline_maintenance_trajectories(evidence):
    """Build review-only decision transitions; this is not a model training dataset."""
    transitions = []
    for decision in evidence.get("recording", {}).get("decisions", []):
        observation = decision.get("observation", {})
        if observation.get("decision_kind") != "skill_maintenance":
            continue
        costs = isolate_maintenance_cost(evidence, decision)
        outcome = costs.get("outcome") or {}
        actual_action = None
        if outcome.get("new_dispatch") is False:
            actual_action = (
                decision["selected_action_id"]
                if decision["selected_action_id"] in {"retain", "no_change"}
                else "no_change"
            )
        elif outcome.get("new_dispatch") is True:
            actual_action = decision["selected_action_id"]
        transitions.append(
            {
                "decision_id": decision["id"],
                "state": observation,
                "action_mask": {
                    action["id"]: action["eligible"] for action in decision["candidates"]
                },
                "selected_action": decision["selected_action_id"],
                "actual_action": actual_action,
                "action_execution_status": outcome.get("status", "unknown"),
                "policy_id": decision["policy_id"],
                "policy_version": decision["policy_version"],
                "selection_kind": decision["selection_kind"],
                "selection_probability": decision.get("selection_probability"),
                "outcome": costs.get("outcome"),
                "costs": costs,
                "technical_result": (costs.get("outcome") or {}).get("technical_status", "unknown"),
                "author_feedback": {"quality": "unknown", "verified_human_evaluation": False},
                "reward": None,
                "training_eligible": False,
                "exclusion_reasons": [
                    "training_permission_not_attested",
                    "reward_not_defined",
                    "behavior_policy_coverage_not_established",
                    "not_token_level_training_data",
                ],
                "source": decision.get("source", {}),
            }
        )
    return {"schema_version": 1, "training_eligible": False, "transitions": transitions}
