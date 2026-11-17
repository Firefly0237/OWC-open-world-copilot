"""Bounded behavior reports and dependency invalidation for immutable draft revisions."""

from __future__ import annotations

from typing import Any

import networkx as nx

from ..content.hash import content_hash
from ..content.models import Quest
from ..logic.explore import explore_quest
from ..logic.simulate import _seed_state, simulate_quest
from ..logic.stepper import advance_preview, preview_view, start_preview
from ..logic.temporal import (
    TemporalSpec,
    _external_input_flags,
    check_soft_lock,
    check_temporal_spec,
)
from .models import WorkflowSpec


def affected_operations(spec: WorkflowSpec, operation: str) -> list[str]:
    node = next((item for item in spec.nodes if item.operation == operation), None)
    if node is None:
        raise ValueError("反馈目标不在当前工作图中")
    graph = nx.DiGraph()
    for item in spec.nodes:
        graph.add_node(item.id)
        graph.add_edges_from(
            (parent, item.id) for parent in item.depends_on + item.optional_dependencies
        )
    affected = nx.descendants(graph, node.id) | {node.id}
    return [item.operation for item in spec.nodes if item.id in affected]


def _scope(
    before: Quest,
    after: Quest,
    initial_state: dict[str, Any] | None,
) -> dict[str, Any]:
    before_state = _seed_state(before.logic, initial_state).as_mapping() if before.logic else {}
    after_state = _seed_state(after.logic, initial_state).as_mapping() if after.logic else {}
    before_symbols = (
        {var.id: var.type.value for var in before.logic.variables} if before.logic else {}
    )
    after_symbols = {var.id: var.type.value for var in after.logic.variables} if after.logic else {}
    return {
        "before_quest_id": before.id,
        "after_quest_id": after.id,
        "initial_policy": "explicit_overrides"
        if initial_state
        else "declared_defaults_per_version",
        "before_initial_state": before_state,
        "after_initial_state": after_state,
        "same_initial_state": before_state == after_state and before_symbols == after_symbols,
        "before_variable_types": before_symbols,
        "after_variable_types": after_symbols,
        "variable_mapping": {key: key for key in sorted(before_symbols.keys() & after_symbols)},
        "external_inputs_before": sorted(_external_input_flags(before.logic))
        if before.logic
        else [],
        "external_inputs_after": sorted(_external_input_flags(after.logic)) if after.logic else [],
        "state_scope": "single_quest_declared_model",
    }


def _strict_replay(
    quest: Quest,
    witness: list[str],
    *,
    initial_state: dict[str, Any] | None,
    max_steps: int,
    expected_outcome: str | None = None,
) -> dict[str, Any]:
    """Replay branch selectors in order through the production explicit-choice stepper."""
    session = start_preview(quest=quest, initial_state=initial_state, max_steps=max_steps)
    consumed = 0
    reason = ""
    status = "withheld"
    while session.status in {"ready", "waiting_choice"}:
        choices = preview_view(session)["choices"]
        choice_id = None
        if choices:
            if consumed == len(witness):
                status, reason = "unexpected_choice", "新版本出现旧见证未指定的分支，不自动选路"
                break
            choice_id = witness[consumed]
            if choice_id not in {choice["id"] for choice in choices}:
                status, reason = "invalid_choice", f"旧选择{choice_id}在当前阶段缺失或条件不满足"
                break
            consumed += 1
        try:
            session = advance_preview(
                session,
                action_id=f"witness-{session.action_seq}",
                choice_id=choice_id,
                expected_seq=session.action_seq,
            )
        except ValueError as exc:
            status, reason = "error", str(exc)
            break
    else:
        if session.status in {"cycle", "truncated", "unsupported"}:
            status, reason = "withheld", session.message or "预演语义或步数范围不足"
        elif consumed < len(witness):
            status, reason = "unconsumed_choices", "旧见证尚有选择未消费，不能视为完整重放"
        elif session.status == "completed":
            status = "replayed"
        else:
            status, reason = "stopped", session.message or f"重放停止于{session.status}"
    return {
        "status": status,
        "reason": reason,
        "requested_choices": witness,
        "consumed_choices": consumed,
        "terminal_status": session.status,
        "outcome": session.outcome,
        "outcome_preserved": (
            session.outcome == expected_outcome
            if status == "replayed" and expected_outcome is not None
            else None
        ),
        "steps": [step.model_dump(mode="json") for step in session.history],
        "final_state": dict(session.values),
    }


def _property_evidence(
    quest: Quest,
    spec: TemporalSpec | None,
    *,
    custom_initial: bool,
    max_states: int,
    max_depth: int,
) -> dict[str, Any]:
    if quest.logic is None or not quest.stages:
        return {"status": "unsupported", "strong": False, "reason": "任务缺少逻辑层或可执行阶段"}
    if custom_initial:
        return {
            "status": "withheld",
            "strong": False,
            "reason": "现有时序检查器不接受自定义初态，未用默认初态结果代替",
        }
    result = (
        check_temporal_spec(quest, spec, max_states=max_states, max_depth=max_depth)
        if spec is not None
        else check_soft_lock(quest, max_states=max_states, max_depth=max_depth)
    )
    return result.model_dump(mode="json")


def build_intent_report(
    before: Quest,
    after: Quest,
    *,
    protected: list[str] | None = None,
    formal_specs: list[TemporalSpec | dict[str, Any]] | None = None,
    initial_state: dict[str, Any] | None = None,
    max_states: int = 2000,
    max_depth: int = 100,
) -> dict[str, Any]:
    """Preserve author text and evaluate only explicitly supplied, supported formal properties."""
    protected_rows = [
        {
            "id": f"author-{index}",
            "text": text,
            "source": "author_protected",
            "status": "withheld",
            "reason": "文本创作意图保留原话，需作者判断，不以模型评分代替",
        }
        for index, text in enumerate(protected or [])
    ]
    raw_specs = (
        formal_specs if formal_specs is not None else before.metadata.get("temporal_specs", [])
    )
    formal: list[dict[str, Any]] = []
    if not isinstance(raw_specs, list):
        formal.append({"status": "error", "reason": "基线temporal_specs不是列表，未静默忽略"})
        raw_specs = []
    for index, raw in enumerate(raw_specs):
        try:
            if isinstance(raw, dict) and set(raw) - set(TemporalSpec.model_fields):
                raise ValueError("规约含未支持的字段或作用域，未自动忽略")
            spec = TemporalSpec.model_validate(raw)
        except ValueError as exc:
            formal.append({"index": index, "status": "error", "reason": str(exc)})
            continue
        old = _property_evidence(
            before,
            spec,
            custom_initial=bool(initial_state),
            max_states=max_states,
            max_depth=max_depth,
        )
        new = _property_evidence(
            after,
            spec,
            custom_initial=bool(initial_state),
            max_states=max_states,
            max_depth=max_depth,
        )
        preserved = old.get("status") == "proven" and new.get("status") == "proven"
        regressed = old.get("status") == "proven" and new.get("status") == "violated"
        formal.append(
            {
                "spec": spec.model_dump(mode="json"),
                "before": old,
                "after": new,
                "status": new["status"],
                "change": "preserved"
                if preserved
                else "regressed"
                if regressed
                else "not_established",
            }
        )
    old_payload, new_payload = before.model_dump(mode="json"), after.model_dump(mode="json")
    return {
        "status": "evidence_only",
        "protected": protected_rows,
        "formal": formal,
        "before_hash": content_hash(before),
        "after_hash": content_hash(after),
        "changed_fields": [key for key in old_payload if old_payload[key] != new_payload[key]],
        "scope": _scope(before, after, initial_state),
    }


def compare_quests(
    before: Quest,
    after: Quest,
    *,
    initial_state: dict[str, Any] | None = None,
    protected: list[str] | None = None,
    formal_specs: list[TemporalSpec | dict[str, Any]] | None = None,
    max_states: int = 2000,
    max_depth: int = 100,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "status": "bounded_comparison",
        "before_hash": content_hash(before),
        "after_hash": content_hash(after),
        "initial_state": initial_state or {},
        "scope": _scope(before, after, initial_state),
        "intent": build_intent_report(
            before,
            after,
            protected=protected,
            formal_specs=formal_specs,
            initial_state=initial_state,
            max_states=max_states,
            max_depth=max_depth,
        ),
        "limits": [
            "只比较单任务声明状态，同名变量按恒等映射；初态差异单独列出",
            "旧见证失效不等于结局不可达；严格重放不使用默认分支替代",
            "结局未到达只限当前初态与探索范围；外部层可能改变输入旗标",
            "文本创作意图未自动证明；循环见证和超过步数的重放保留未知",
        ],
    }
    if before.logic is None or after.logic is None:
        return {**report, "status": "unsupported", "reason": "前后版本都需要显式任务逻辑"}
    old_run = simulate_quest(before, initial_state=initial_state, max_steps=max_depth)
    witness = [step.branch_taken for step in old_run.steps if step.branch_taken]
    last_branch = next(
        (
            branch
            for branch in before.logic.branches
            if witness and branch.id == witness[-1] and not branch.to_stage
        ),
        None,
    )
    report["old_witness"] = old_run.model_dump(mode="json")
    report["replay"] = _strict_replay(
        after,
        witness,
        initial_state=initial_state,
        max_steps=max_depth,
        expected_outcome=last_branch.outcome if last_branch else None,
    )
    report["soft_lock"] = {
        side: _property_evidence(
            quest,
            None,
            custom_initial=bool(initial_state),
            max_states=max_states,
            max_depth=max_depth,
        )
        for side, quest in (("before", before), ("after", after))
    }
    if initial_state:
        return {
            **report,
            "status": "partial_comparison",
            "truncated": None,
            "before": {"status": "withheld", "reason": "有界探索不支持自定义初态"},
            "after": {"status": "withheld", "reason": "有界探索不支持自定义初态"},
        }
    old = explore_quest(before, max_states=max_states, max_depth=max_depth)
    new = explore_quest(after, max_states=max_states, max_depth=max_depth)
    old_reached = {
        (item.kind, item.branch_id, item.outcome) for item in old.outcomes if item.reachable
    }
    new_reached = {
        (item.kind, item.branch_id, item.outcome) for item in new.outcomes if item.reachable
    }
    report.update(
        {
            "before": old.model_dump(mode="json"),
            "after": new.model_dump(mode="json"),
            "truncated": old.truncated or new.truncated,
            "outcome_changes": {
                "same_initial_state": report["scope"]["same_initial_state"],
                "newly_reached": sorted(new_reached - old_reached),
                "no_longer_reached": sorted(old_reached - new_reached),
                "claim": "not_reached_in_bound" if new.truncated else "declared_default_model_only",
            },
            "witness_replays": [
                {
                    "outcome": item.outcome,
                    "branch_id": item.branch_id,
                    "replay": _strict_replay(
                        after,
                        item.witness,
                        initial_state=None,
                        max_steps=max_depth,
                        expected_outcome=item.outcome if item.kind == "terminal" else None,
                    ),
                }
                for item in [row for row in old.outcomes if row.reachable][:20]
            ],
        }
    )
    return report
