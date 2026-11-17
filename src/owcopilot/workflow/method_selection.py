"""Bounded method selection using shared candidates and observed, explicit costs."""

from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass
from typing import Protocol

from ..content.hash import content_hash
from .method_assets import bind_asset, derive_assets
from .v2_planning import validate_requirement_capabilities

_MODEL_ROLES = {
    "plan_events",
    "draft_quest",
    "draft_logic",
    "logic_contract",
    "draft_dialogue",
    "dialogue_constrained",
}
_REQUIRED_CHECKS = [
    "current_permissions",
    "source_and_version_binding",
    "typed_ports",
    "requirement_capabilities",
    "all_role_requirements",
    "published_coverage",
]


@dataclass(frozen=True)
class MethodAction:
    id: str
    mode: str
    asset: dict | None
    adaptation: str | None
    graph: dict | None
    verification_plan: dict
    estimated_tokens: int | None
    cost_parts: dict
    dispatch_reservation: int
    eligible: bool = True
    rejection_reasons: tuple[str, ...] = ()
    retrieval_score: float = 0.0
    binding: dict | None = None

    def receipt(self) -> dict:
        return {
            "id": self.id,
            "mode": self.mode,
            "asset": self.asset,
            "adaptation": self.adaptation,
            "graph_hash": content_hash(self.graph) if self.graph else None,
            "verification_plan": self.verification_plan,
            "estimated_tokens": self.estimated_tokens,
            "cost_parts": self.cost_parts,
            "dispatch_reservation": self.dispatch_reservation,
            "eligible": self.eligible,
            "rejection_reasons": list(self.rejection_reasons),
            "retrieval_score": self.retrieval_score,
        }


class DecisionPolicy(Protocol):
    id: str
    version: str

    def select(
        self, observation: dict, feasible_actions: list[MethodAction], budget: dict
    ) -> MethodAction | None: ...


def _affordable(action, budget):
    cap = budget.get("remaining_dispatches")
    tokens = budget.get("remaining_tokens")
    return (
        action.eligible
        and (cap is None or action.dispatch_reservation <= cap)
        and (
            tokens is None
            or action.estimated_tokens is not None
            and action.estimated_tokens <= tokens
        )
    )


def _rank(action):
    return (
        action.estimated_tokens,
        len((action.binding or {}).get("bound_nodes", (action.asset or {}).get("node_ids", []))),
        action.id,
    )


class JointRulePolicy:
    id = "complete_action_cost"
    version = "1"

    def select(self, observation, feasible_actions, budget):
        allowed = [a for a in feasible_actions if _affordable(a, budget)]
        measured = [a for a in allowed if a.estimated_tokens is not None]
        if measured:
            return min(measured, key=_rank)
        # Unknown reuse costs are not zero. The fresh path remains explicitly unknown
        # and bounded by the formal dispatch/output policy, never a claimed saving.
        return next((a for a in allowed if a.mode == "fresh"), None)


class StagedLookaheadPolicy(JointRulePolicy):
    """Strong staged control sees complete action costs before choosing an asset."""

    id = "asset_then_scope_with_complete_costs"

    def select(self, observation, feasible_actions, budget):
        groups: dict[str, list[MethodAction]] = {}
        for action in feasible_actions:
            if _affordable(action, budget) and action.estimated_tokens is not None:
                groups.setdefault((action.asset or {}).get("id", "fresh"), []).append(action)
        if not groups:
            return super().select(observation, feasible_actions, budget)
        # This may be equivalent to complete-action enumeration; preserve that
        # negative result rather than weakening the comparison to force a gain.
        group = min(groups.values(), key=lambda rows: _rank(min(rows, key=_rank)))
        return min(group, key=_rank)


class SkillRulePolicy(JointRulePolicy):
    id = "skills_and_fresh"

    def select(self, observation, feasible_actions, budget):
        return super().select(
            observation,
            [a for a in feasible_actions if a.asset is None or a.asset["kind"] == "skill"],
            budget,
        )


class FreshPlanningPolicy(JointRulePolicy):
    id = "fresh_planning"

    def select(self, observation, feasible_actions, budget):
        return super().select(
            observation, [a for a in feasible_actions if a.mode == "fresh"], budget
        )


def observed_role_costs(store, project: str, model: str | None) -> dict:
    """A forecast from local observations, not a bound or a quality prediction."""
    samples: dict[str, list[int]] = {}
    with store.connection() as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"creative_harness_dispatches", "creative_harness_events"} <= tables:
            return {}
        rows = db.execute(
            "SELECT d.request,e.payload FROM creative_harness_dispatches d "
            "JOIN runs r ON r.id=d.run_id JOIN tasks t ON t.id=r.task_id "
            "JOIN creative_harness_events e ON e.run_id=d.run_id AND e.event_type='model/result' "
            "AND json_extract(e.payload,'$.payload.request_id')=d.id "
            "WHERE json_extract(t.payload,'$.project')=? ORDER BY d.rowid DESC LIMIT 2000",
            (project,),
        ).fetchall()
    seen = set()
    for row in rows:
        request, event = json.loads(row[0]), json.loads(row[1])
        if request["id"] in seen:
            continue
        seen.add(request["id"])
        if model and request["request"].get("model") != model:
            continue
        if request["request"].get("config", {}).get("execution_origin") == "controlled_transport":
            continue
        outcome = event.get("payload", {})
        usage = outcome.get("measurement", {})
        values = [usage.get("input_tokens"), usage.get("output_tokens")]
        if outcome.get("status") != "done" or not all(type(v) is int and v >= 0 for v in values):
            continue
        role = request["request"].get("role_id")
        if role:
            samples.setdefault(role, []).append(sum(values))
    return {
        role: {"median_tokens": math.ceil(statistics.median(values)), "samples": len(values)}
        for role, values in samples.items()
    }


def _estimate(roles: list[str], stats: dict, *, fresh=False):
    requested = roles + (["initial_planner"] if fresh else [])
    missing = sorted(set(requested) - stats.keys())
    if missing:
        return None, {
            "basis": "missing_observations",
            "missing_roles": missing,
            "future_maintenance": "unknown_not_claimed",
        }
    execution = sum(stats[role]["median_tokens"] for role in roles)
    planning = stats["initial_planner"]["median_tokens"] if fresh else 0
    reserve = max((stats[role]["median_tokens"] for role in requested), default=0)
    return execution + planning + reserve, {
        "basis": "observed_role_medians_plus_one_call_reserve",
        "execution": execution,
        "planning": planning,
        "rescue_reserve": reserve,
        "retrieval_model_tokens": 0,
        "registered_adaptation_model_tokens": 0,
        "formal_check_model_tokens": 0,
        "future_maintenance": "unknown_not_claimed",
        "scope": "next_task_forecast_not_lifetime_guarantee",
    }


def enumerate_actions(
    matches: list[dict], requirements: dict | None, role_costs: dict, *, max_assets=3
) -> list[MethodAction]:
    assets = []
    # Preserve grain diversity within a documented, shared bounded pool.
    derived = []
    rejected = []
    for row in matches[:max_assets]:
        try:
            derived.extend((row, asset) for asset in derive_assets(row["version"]))
        except ValueError as exc:
            rejected.append(
                MethodAction(
                    id="invalid-" + row["version_id"],
                    mode="reuse",
                    asset={"source_version_id": row["version_id"], "kind": "graph"},
                    adaptation=None,
                    graph=None,
                    verification_plan={"required": _REQUIRED_CHECKS, "optional": []},
                    estimated_tokens=None,
                    cost_parts={"basis": "not_admitted"},
                    dispatch_reservation=33,
                    eligible=False,
                    rejection_reasons=(str(exc),),
                )
            )
    for kind in ("graph", "subgraph", "skill"):
        found = next(((row, asset) for row, asset in derived if asset.kind == kind), None)
        if found:
            assets.append(found)
    assets = assets[:max_assets]
    actions = list(rejected)
    for row, asset in assets:
        for adaptation in (None, "decision_contract"):
            binding = None
            graph = None
            reasons = []
            estimate, parts = None, {"basis": "not_admitted"}
            try:
                graph, binding = bind_asset(row["version"], asset, adaptation)
                validate_requirement_capabilities(graph, requirements)
                roles = [node.operation for node in graph.nodes if node.operation in _MODEL_ROLES]
                estimate, parts = _estimate(roles, role_costs)
            except ValueError as exc:
                reasons.append(str(exc))
            if not row["eligible_to_execute"]:
                reasons.append("方法条件冲突或尚未确认")
            if row.get("score", 0) <= 0:
                reasons.append("没有检索相关依据")
            mode = "adapt" if adaptation else "reuse"
            plan = {
                "required": _REQUIRED_CHECKS,
                "optional": [],
                "scope_nodes": [n.id for n in graph.nodes] if graph else [],
                "affected_nodes": (binding or {}).get("adaptation", {}).get("affected_nodes", []),
                "semantic_coverage": "not_proven",
            }
            actions.append(
                MethodAction(
                    id=content_hash([asset.id, adaptation, plan]),
                    mode=mode,
                    asset=asset.model_dump(),
                    adaptation=adaptation,
                    graph=graph.model_dump(mode="json") if graph else None,
                    verification_plan=plan,
                    estimated_tokens=estimate,
                    cost_parts=parts,
                    dispatch_reservation=len(
                        [n for n in graph.nodes if n.operation in _MODEL_ROLES]
                    )
                    + 1
                    if graph
                    else 33,
                    eligible=not reasons,
                    rejection_reasons=tuple(reasons),
                    retrieval_score=row.get("score", 0),
                    binding=binding,
                )
            )
    # A fresh plan is unobserved until the real planner returns. It is not a seed-graph fallback.
    roles = ["plan_events", "draft_quest", "logic_contract", "dialogue_constrained"]
    estimate, parts = _estimate(roles, role_costs, fresh=True)
    actions.append(
        MethodAction(
            id="fresh",
            mode="fresh",
            asset=None,
            adaptation=None,
            graph=None,
            verification_plan={
                "required": _REQUIRED_CHECKS,
                "optional": [],
                "scope_nodes": "actual_planned_graph",
            },
            estimated_tokens=estimate,
            cost_parts=parts,
            dispatch_reservation=33,
        )
    )
    return actions
