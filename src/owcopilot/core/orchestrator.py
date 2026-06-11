"""The PLAN-EXECUTE-VERIFY loop (with a conditional REPAIR), built on LangGraph.

Flow:
    PLAN -> EXECUTE -> VERIFY --(no errors)--> DONE
                          ^                     |
                          |                  (errors & attempts left)
                          +------- REPAIR <-----+

The node functions are intentionally small and dependency-injected so P1 can swap the
mock generator / deterministic repair for real LLM-backed implementations without
changing the graph wiring.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from langgraph.graph import END, StateGraph

from .state import Phase, TaskState, ValidationIssue


def build_graph(
    *,
    gateway,  # llm.gateway.LLMGateway
    generator,  # core.protocols.Generator
    validators: Sequence[Callable[[dict], list[ValidationIssue]]],
    repair_strategy,  # consistency.repair.RepairStrategy
    adapter=None,  # core.protocols.EngineAdapter | None
):
    def plan_node(state: TaskState) -> dict:
        intent = state["intent"]
        # A cheap model decomposes the intent (P0: routed to the 'cheap' tier; fixed plan).
        gateway.complete(
            task="plan",
            system="You are a planner for game content tasks.",
            user=f"Decompose into steps: {intent}",
        )
        plan = ["retrieve_lore", "generate_quest", "land_to_engine"]
        return {"phase": Phase.PLAN, "plan": plan, "log": [f"PLAN: produced {len(plan)} steps"]}

    def execute_node(state: TaskState) -> dict:
        artifact = generator.generate(state["intent"])  # routed to 'generate' (frontier) inside
        if adapter is not None:
            adapter.apply(artifact)
        return {
            "phase": Phase.EXECUTE,
            "artifact": artifact,
            "log": ["EXECUTE: generated artifact + landed to engine (mock)"],
        }

    def verify_node(state: TaskState) -> dict:
        issues: list[ValidationIssue] = []
        for v in validators:
            issues.extend(v(state.get("artifact") or {}))
        n_err = sum(1 for i in issues if i.severity == "error")
        return {
            "phase": Phase.VERIFY,
            "issues": issues,
            "log": [f"VERIFY: {len(issues)} issue(s), {n_err} error(s)"],
        }

    def repair_node(state: TaskState) -> dict:
        attempts = state.get("repair_attempts", 0) + 1
        issues = state.get("issues", [])
        # The repair STRATEGY owns whether a model is called: deterministic remap (zero-token)
        # or an LLM-backed localised fix that routes through the gateway. The node stays
        # strategy-agnostic so P1 can swap RepairStrategy -> LLMRepairStrategy with no rewiring.
        fixed = repair_strategy.repair(state.get("artifact") or {}, issues)
        return {
            "phase": Phase.REPAIR,
            "artifact": fixed,
            "issues": [],
            "repair_attempts": attempts,
            "log": [f"REPAIR: attempt {attempts} applied"],
        }

    def route_after_verify(state: TaskState) -> str:
        errors = [i for i in state.get("issues", []) if i.severity == "error"]
        if not errors:
            return "done"
        if state.get("repair_attempts", 0) >= state.get("max_repair_attempts", 2):
            return "failed"
        return "repair"

    g = StateGraph(TaskState)
    g.add_node("plan", plan_node)
    g.add_node("execute", execute_node)
    g.add_node("verify", verify_node)
    g.add_node("repair", repair_node)
    g.set_entry_point("plan")
    g.add_edge("plan", "execute")
    g.add_edge("execute", "verify")
    g.add_conditional_edges(
        "verify", route_after_verify, {"done": END, "failed": END, "repair": "repair"}
    )
    g.add_edge("repair", "verify")
    return g.compile()
