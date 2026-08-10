"""Shared multi-agent assembly used by CLI and the FastAPI async-job surface."""

from __future__ import annotations

import math
import threading
import uuid
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from ...config import DEFAULT_CHEAP_MODEL, DEFAULT_LLM_MODE
from ...llm.telemetry import TelemetryCollector
from ...telemetry import llm_step, summarize_workflow
from .._common import open_project
from .agent import build_agent_chat_model


def _patch_ids_from_tool_result(value: Any) -> set[str]:
    """Extract persisted patch IDs from nested propose/harness tool output."""
    found: set[str] = set()
    if isinstance(value, dict):
        patch_id = value.get("patch_id")
        if isinstance(patch_id, str) and patch_id:
            found.add(patch_id)
        for nested in value.values():
            found.update(_patch_ids_from_tool_result(nested))
    elif isinstance(value, list):
        for nested in value:
            found.update(_patch_ids_from_tool_result(nested))
    return found


def run_multi_agent_action(
    content_root: str | Path,
    *,
    goal: str,
    max_tool_calls: int | None = None,
    max_total_tool_calls: int | None = None,
    max_model_calls: int | None = None,
    checkpoint_path: str | None = None,
    llm_mode: str = DEFAULT_LLM_MODE,
    llm_model: str = DEFAULT_CHEAP_MODEL,
    sqlite_path: str | None = None,
    max_cost_usd: float | None = None,
    chat_model: Any | None = None,
    progress: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Run orchestrator → workers → verifier without exposing filesystem paths over REST.

    The session remains propose-only. Its blackboard flow and independent verifier verdicts are
    returned as first-class evidence. Tool calls retain a per-worker cap and also share one
    session-wide execution ledger; model calls share a second ledger. The optional dollar threshold
    blocks subsequent dispatches once settled telemetry reaches it.
    """

    from ...mcp_server import tools as tools_mod
    from ...multi_agent import MultiAgentSession
    from ...multi_agent.orchestrator import MAX_DECOMPOSED_SUBTASKS
    from ...multi_agent.session import (
        DEFAULT_SESSION_MAX_MODEL_CALLS,
        DEFAULT_SESSION_MAX_TOOL_CALLS,
    )
    from ...retrieval.embedding import resolve_embedder
    from ...skills import SkillRegistry, default_skill_registry
    from ...storage.checkpointer import CheckpointStore

    root = Path(content_root)
    if not goal.strip():
        raise ValueError("目标（goal）不能为空，请描述多智能体需要共同完成的诊断任务。")
    if max_tool_calls is not None and max_tool_calls < 1:
        raise ValueError("max_tool_calls 必须大于等于 1。")
    if max_total_tool_calls is not None and max_total_tool_calls < 1:
        raise ValueError("max_total_tool_calls 必须大于等于 1。")
    if max_model_calls is not None and max_model_calls < 1:
        raise ValueError("max_model_calls 必须大于等于 1。")
    if max_cost_usd is not None and (not math.isfinite(max_cost_usd) or max_cost_usd < 0):
        raise ValueError("max_cost_usd 必须是大于等于 0 的有限数值。")
    emit = progress or (lambda _type, _data: None)
    emit("stage", {"stage": "accepted"})

    resolved_sqlite = str(sqlite_path or root / ".owcopilot" / "runtime.sqlite")
    resolved_checkpoint = str(checkpoint_path or root / ".owcopilot" / "checkpoints.sqlite")
    checkpoint_store = CheckpointStore(resolved_checkpoint)
    checkpoint_store.sweep()
    thread_id = str(uuid.uuid4())
    checkpoint_store.record_thread(thread_id, source="multi-agent")
    telemetry = TelemetryCollector()
    model = chat_model or build_agent_chat_model(llm_mode=llm_mode, llm_model=llm_model)

    try:
        with open_project(root, resolved_sqlite) as project:
            telemetry.record_sink = project.sqlite_store.record_telemetry
            token = tools_mod._shared_project.set(project)
            try:
                base_registry = default_skill_registry(
                    content_root=str(root), sqlite_path=resolved_sqlite
                )
                proposal_ids: set[str] = set()
                proposal_ids_lock = threading.Lock()
                registry = SkillRegistry()
                for skill in base_registry:
                    if skill.name not in {"propose_fix", "quality_harness"}:
                        registry.register(skill)
                        continue

                    original_handler = skill.handler

                    def tracked_handler(
                        *, _handler: Callable[..., dict[str, Any]] = original_handler, **kwargs: Any
                    ) -> dict[str, Any]:
                        result = _handler(**kwargs)
                        with proposal_ids_lock:
                            proposal_ids.update(_patch_ids_from_tool_result(result))
                        return result

                    registry.register(replace(skill, handler=tracked_handler))
                session = MultiAgentSession(
                    orchestrator_model=model,
                    model=model,
                    registry=registry,
                    db_path=resolved_sqlite,
                    per_agent_max_tool_calls=max_tool_calls,
                    session_max_model_calls=(
                        max_model_calls
                        if max_model_calls is not None
                        else DEFAULT_SESSION_MAX_MODEL_CALLS
                    ),
                    session_max_tool_calls=(
                        max_total_tool_calls
                        if max_total_tool_calls is not None
                        else DEFAULT_SESSION_MAX_TOOL_CALLS
                    ),
                    session_max_cost_usd=max_cost_usd,
                    store=project.sqlite_store,
                    embedder=resolve_embedder(),
                    telemetry=telemetry,
                    checkpointer=checkpoint_store.saver,
                )
                try:
                    emit("stage", {"stage": "orchestrate"})
                    report = session.run(goal.strip(), thread_id=thread_id)
                    session_budget = session.session_budget
                    flow = [
                        {
                            "msg_type": message.msg_type,
                            "from_agent": message.from_agent,
                            "to_agent": message.to_agent,
                            "status": message.status,
                        }
                        for message in session.blackboard.session_flow(report.session_id)
                    ]
                finally:
                    session.close()
                proposals = [
                    {**patch, "patch_id": patch["id"]}
                    for patch_id in sorted(proposal_ids)
                    if (patch := project.sqlite_store.get_patch(patch_id)) is not None
                    and patch["status"] == "proposed"
                ]
            finally:
                tools_mod._shared_project.reset(token)
                telemetry.record_sink = None
    finally:
        checkpoint_store.close()

    emit("stage", {"stage": "complete"})
    telemetry_summary = telemetry.summary()
    cost_budget = summarize_workflow(
        [llm_step("multi_agent", telemetry_summary)], budget_usd=max_cost_usd
    ).budget.model_dump(mode="json")
    return {
        "session_id": report.session_id,
        "goal": report.goal,
        "participants": report.participants,
        "blackboard_flow": flow,
        "worker_summaries": report.worker_summaries,
        "verifier_verdicts": report.verifier_verdicts,
        "proposals": proposals,
        "synthesis": report.synthesis,
        "decomposition_degraded": report.decomposition_degraded,
        "llm_mode": llm_mode,
        "thread_id": thread_id,
        "blackboard_storage": "memory" if resolved_sqlite == ":memory:" else "persistent_sqlite",
        "budget_scope": {
            "max_tool_calls": "per_subtask_or_verifier_run",
            "per_agent_max_tool_calls": max_tool_calls,
            "max_decomposed_subtasks": MAX_DECOMPOSED_SUBTASKS,
            "session_global_model_budget": session_budget,
            "session_global_tool_budget": {
                key: value for key, value in session_budget.items() if key.startswith("tool_calls_")
            },
            "session_global_budget_status": (
                "hard_model_and_tool_call_ledgers_with_settled_cost_gate"
            ),
            "max_cost_usd": "settled_cost_blocks_next_dispatch",
        },
        "telemetry": telemetry_summary,
        "cost_budget": cost_budget,
    }
