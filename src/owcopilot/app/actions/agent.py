# agent 诊断 run 的动作层：CLI 与 REST 异步 job 共用的唯一装配路径（E1/N8.4）。
# 要点:① 装配只有这一份——CLI 的 `owcopilot agent` 与 job kind "agent_diagnose" 调同一个函数,
#     不存在第二条 agent 执行路径;② 动作空间仍是只读/提议,永不写 canon(人审队列是唯一写入口);
#     ③ HITL 复用 T6 的 interrupt + T4 的 checkpointer——一次暂停就是一次 job 的正常结束,
#     人类的批准通过"再提交一个 resume job"送回去,不另造中断协议。
"""The agent diagnosis run as one UI-agnostic action.

``owcopilot agent`` (CLI) and the ``agent_diagnose`` async job (REST → the Vue page) are two
front-ends over :func:`run_agent_diagnose_action`; the assembly it performs — checkpoint store,
thread-id policy, scoped skill registry, episodic recall, HITL gate, run persistence, cost
accounting — exists exactly once, so the two surfaces cannot drift.

What this action does NOT do is as load-bearing as what it does: its permission surface always
starts from :func:`~owcopilot.skills.builtin.default_skill_registry`; the explicit MCP route swaps
only the execution transport of its existing ``audit_project`` capability before the same scope is
applied. Those skills are read-only except the one ``PROPOSES_PATCH`` skill, and *no* skill writes
canon. Approving a HITL gate therefore authorises a *proposal* into the review queue, never a canon
write — the human review path remains the only writer, on the REST/UI surface exactly as on the CLI.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from ...config import DEFAULT_CHEAP_MODEL, DEFAULT_LLM_MODE
from ...llm.gateway import require_offline_llm_allowed
from ...llm.telemetry import TelemetryCollector
from ...telemetry import llm_step, summarize_workflow
from ...util import load_dotenv
from .._common import open_project

if TYPE_CHECKING:
    from ...mcp_client import MCPInvocationTrace, MCPToolClient

#: The human decisions a paused HITL gate accepts on this surface. The middleware also understands
#: ``edit`` (rewrite the pending call's arguments), which neither front-end offers: editing a tool
#: call is an authoring act, and the product answer to "the proposal is wrong" is reject + re-run.
AGENT_HITL_DECISIONS = ("approve", "reject")

#: Coarse progress stages emitted as ``("stage", {"stage": ...})`` through the ``progress``
#: callback, in order. The ReAct loop itself is ONE blocking ``invoke`` (its per-step callbacks are
#: internal to :class:`~owcopilot.agent.react.ReActAgent`), so these mark the assembly boundaries a
#: caller can really observe — no faked per-tool-call tick.
AGENT_PROGRESS_STAGES = ("accepted", "recall", "reason", "persist")


def build_agent_chat_model(*, llm_mode: str, llm_model: str) -> Any:
    """The agent's LangChain chat model per mode — the ``create_agent`` assembly point.

    real: taken from :class:`~owcopilot.llm.chat_model.ChatModelProvider` (the ONE place that knows
    how the real model is configured — env, key resolution, output cap), then wrapped in
    ``build_chat_model_with_resilience`` (always adds the whole-chain deadline/attempt guard;
    failover and circuit breakers remain env opt-in). offline: the deterministic ``tool_calls``
    double, a test/CI dry-run gated behind ``OWCOPILOT_ALLOW_OFFLINE_LLM`` — never a shipped mode.
    The retired text-path json_mode knob does not apply here: the agent's planning calls never pass
    the provider's JSON gate.
    """
    if llm_mode == "real":
        load_dotenv()  # pick up OPENAI_BASE_URL / OPENAI_API_KEY from .env; shell env wins
        from ...llm.chat_model import (  # noqa: PLC0415 — live path only
            ChatModelProvider,
            build_chat_model_with_resilience,
        )

        return build_chat_model_with_resilience(ChatModelProvider(model=llm_model).chat_model())
    require_offline_llm_allowed()  # offline is a test/CI dry-run, not a product mode
    from ...agent.offline import OfflineReactChatModel  # noqa: PLC0415

    return OfflineReactChatModel()


def resolve_agent_thread_id(store: Any, *, thread_id: str | None, resume: bool) -> str:
    """Resolve the checkpoint thread id for one agent run (guided errors, §5.2 matrix).

    Always returns a concrete id (the checkpointer is always-on): a resume requires an existing
    thread, an explicit id that already carries a checkpoint is refused without ``resume`` (never a
    silent overwrite / chat-memory accumulation), and the default fresh run mints a unique id.
    """
    if resume:
        if not thread_id:
            raise ValueError(
                "续跑需要指出要继续的那次 run 的 thread_id；单独的 resume 无法定位 checkpoint。"
            )
        if not store.thread_exists(thread_id):
            raise ValueError(
                f"找不到 thread '{thread_id}' 的可续 checkpoint；"
                f"请先用这个 thread_id 跑一次，或核对 id 是否正确。"
            )
        return thread_id
    if thread_id:
        if store.thread_exists(thread_id):
            raise ValueError(
                f"thread '{thread_id}' 已存在 checkpoint；续跑请带上 resume，或换一个 thread_id。"
            )
        return thread_id
    return str(uuid.uuid4())


def hitl_decisions(
    decision: str | None, decision_message: str | None = None
) -> list[dict[str, Any]] | None:
    """The HITL decision list for a resume, or ``None`` for a plain (T4 crash) resume.

    Maps the single human decision to one ``HITLRequest`` decision; the agent replicates it across
    every action in the pending gate. No decision ⇒ ``None`` so a bare resume stays the T4
    crash-continue path.
    """
    if not decision:
        return None
    entry: dict[str, Any] = {"type": decision}
    if decision == "reject" and decision_message:
        entry["message"] = decision_message
    return [entry]


def resolve_agent_tool_transport(
    checkpoint_store: Any,
    *,
    thread_id: str,
    resume: bool,
    requested: Literal["native", "mcp"] | None,
) -> Literal["native", "mcp"]:
    """Choose the fresh route or restore the route frozen in a checkpoint contract.

    Route selection happens before the scoped registry is built, so this reads the same
    ``owc_run_contract`` channel that :class:`ReActAgent` subsequently validates. A resume may
    omit the route and inherit it; explicitly changing it is a typed contract mismatch rather
    than a silent MCP/native fallback.
    """
    if requested is not None and requested not in ("native", "mcp"):
        raise ValueError(
            "工具传输（tool_transport）只能是 native 或 mcp，"
            f"收到的是 {requested!r}。"
        )
    if not resume:
        return requested or "native"

    from ...agent.middleware import AgentRunContractError  # noqa: PLC0415

    checkpoint = checkpoint_store.saver.get({"configurable": {"thread_id": thread_id}})
    values = checkpoint.get("channel_values", {}) if isinstance(checkpoint, dict) else {}
    contract = values.get("owc_run_contract") if isinstance(values, dict) else None
    if not isinstance(contract, dict):
        raise AgentRunContractError(
            "checkpoint predates the versioned run-contract channel; start a fresh run"
        )
    stored = contract.get("tool_transport")
    if stored not in ("native", "mcp"):
        raise AgentRunContractError(
            "checkpoint run contract has no valid tool_transport; start a fresh run"
        )
    if requested is not None and requested != stored:
        raise AgentRunContractError(
            "resume cannot change tool_transport: "
            f"checkpoint={stored!r}, requested={requested!r}"
        )
    return cast("Literal['native', 'mcp']", stored)


def run_agent_diagnose_action(
    content_root: str | Path,
    *,
    goal: str = "",
    max_steps: int | None = None,
    max_model_calls: int | None = None,
    max_tool_calls: int | None = None,
    skills: list[str] | None = None,
    recall: bool = True,
    hitl: bool = False,
    early_stopping: str = "force",
    thread_id: str | None = None,
    resume: bool = False,
    decision: str | None = None,
    decision_message: str | None = None,
    checkpoint_path: str | None = None,
    llm_mode: str = DEFAULT_LLM_MODE,
    llm_model: str = DEFAULT_CHEAP_MODEL,
    sqlite_path: str | None = None,
    max_cost_usd: float | None = None,
    tool_transport: Literal["native", "mcp"] | None = None,
    chat_model: Any | None = None,
    mcp_client: MCPToolClient | None = None,
    progress: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Run one bounded ReAct diagnosis over a project and return its JSON-able payload.

    ``skills`` is the execution allow-list (``None`` = the whole built-in registry); it is wired
    into a :func:`~owcopilot.skills.scoped_registry` so it gates EXECUTION, not just what the model
    is shown. Names the registry does not know are reported back as ``unknown_skills`` (a warning,
    not a failure — the run proceeds on the known subset).

    ``hitl`` arms the T6 inline-interrupt gate on the sensitive (``PROPOSES_PATCH``) skills. A run
    that hits the gate returns ``status="interrupted"`` with ``thread_id`` + ``interrupt_request``
    and *no* final answer; the human answers by calling this action again with that ``thread_id``,
    ``resume=True`` and ``decision="approve"|"reject"``. Nothing about the gate is bespoke: the
    interrupt is LangGraph's, the state lives in the T4 checkpointer, and the "answer" is just the
    next invocation — no second suspend/resume protocol.

    ``chat_model`` is the model-injection point: pass an already-assembled LangChain chat model
    (the CLI does — it resolves ``--llm-mode``/``--llm-model`` through argparse's env-aware
    defaults), or leave it ``None`` and this builds one from ``llm_mode``/``llm_model`` via
    :func:`build_agent_chat_model` (what the REST job does with the request's params).

    ``tool_transport`` is an explicit execution choice. A fresh run defaults to ``native``;
    ``mcp`` replaces only ``audit_project`` with the checked local stdio MCP consumer before the
    same skill allow-list is applied. On resume, omission restores the checkpointed route while an
    explicit change is rejected as ``RUN_CONTRACT_MISMATCH``. MCP failures stay on that route and
    are never hidden by a native fallback. ``mcp_client`` is an in-process test seam and is
    intentionally not exposed by either the CLI or REST contract.

    Raises :class:`ValueError` with Chinese guidance for the misuse cases (blank goal on a fresh
    run, a decision without a resume, a resume without a resumable thread).
    """
    from ...agent import ReActAgent, episodic, persist_agent_run
    from ...mcp_server import tools as tools_mod
    from ...retrieval.embedding import resolve_embedder
    from ...skills import SideEffect, default_skill_registry, scoped_registry
    from ...storage.checkpointer import CheckpointStore

    root = Path(content_root)
    if not root.exists():
        raise FileNotFoundError(f"content root does not exist: {root}")
    if not resume and not goal.strip():
        raise ValueError("目标（goal）不能为空，请描述 agent 需要诊断什么。例如：检查任务链一致性")
    if decision and not resume:
        raise ValueError(
            "决定（approve/reject）只用于恢复一次暂停在人工审批门的 run；"
            "请连同该 run 的 thread_id 一起以续跑方式提交，或去掉这个决定。"
        )
    if decision and decision not in AGENT_HITL_DECISIONS:
        raise ValueError(
            f"决定只能是 {' 或 '.join(AGENT_HITL_DECISIONS)}，收到的是 {decision!r}。"
            "批准会让 agent 执行它提议的那一步（仍只产出提案，不写正典），拒绝则跳过并告知模型。"
        )
    if early_stopping not in ("force", "generate"):
        raise ValueError(
            f"步数耗尽策略（early_stopping）只能是 force 或 generate，收到的是 {early_stopping!r}。"
        )
    if tool_transport is not None and tool_transport not in ("native", "mcp"):
        raise ValueError(
            "工具传输（tool_transport）只能是 native 或 mcp，"
            f"收到的是 {tool_transport!r}。"
        )
    emit = progress or (lambda _type, _data: None)
    emit("stage", {"stage": "accepted"})
    resolved_sqlite = str(sqlite_path or root / ".owcopilot" / "runtime.sqlite")

    # B always-on: a checkpointer is attached to EVERY run, on an independent file, with an
    # auto-minted unique thread id — so any run is resumable and a fresh thread keeps the produced
    # AgentResult byte-identical to a no-checkpointer run. A per-run lifecycle sweep bounds it.
    resolved_checkpoint = str(checkpoint_path or root / ".owcopilot" / "checkpoints.sqlite")
    checkpoint_store = CheckpointStore(resolved_checkpoint)
    try:
        checkpoint_store.sweep()  # opportunistic TTL/cap cleanup
        effective_thread_id = resolve_agent_thread_id(
            checkpoint_store, thread_id=thread_id, resume=resume
        )
        checkpoint_store.record_thread(effective_thread_id, source="agent")
        effective_tool_transport = resolve_agent_tool_transport(
            checkpoint_store,
            thread_id=effective_thread_id,
            resume=resume,
            requested=tool_transport,
        )
    except Exception:
        # A guided validation error (dup/missing thread) must not leak the connection.
        checkpoint_store.close()
        raise

    model = (
        chat_model
        if chat_model is not None
        else build_agent_chat_model(llm_mode=llm_mode, llm_model=llm_model)
    )
    # One collector for the whole run: the agent attaches it via the standard callback surface,
    # so planning, compaction and generate-finish calls all land in the same summary().
    telemetry = TelemetryCollector()
    unknown_skills: list[str] = []
    known_skills: list[str] = []
    mcp_traces: list[MCPInvocationTrace] = []
    agent_run_id: str | None = None
    try:
        # One shared ProjectContext for the whole ReAct run so tool calls reuse the same parsed
        # project state and see writes from earlier steps.
        with open_project(root, resolved_sqlite) as project:
            # Flush each callback record immediately. If model invocation raises and the action
            # unwinds, the sanitized failure row is already durable before the store closes.
            telemetry.record_sink = project.sqlite_store.record_telemetry
            token = tools_mod._shared_project.set(project)
            try:
                registry = default_skill_registry(
                    content_root=str(root), sqlite_path=resolved_sqlite
                )
                if effective_tool_transport == "mcp":
                    from ...mcp_client import (  # noqa: PLC0415 — selected product route only
                        default_local_config,
                        registry_with_mcp_audit,
                    )

                    registry = registry_with_mcp_audit(
                        registry,
                        default_local_config(
                            content_root=root,
                            sqlite_path=resolved_sqlite,
                            source_root=Path(__file__).resolve().parents[3],
                        ),
                        client=mcp_client,
                        trace_sink=mcp_traces.append,
                    )
                allowed_skills = set(skills) if skills is not None else None
                # Cross-validate the allow-list; unknown names are surfaced (not fatal) so a typo
                # or a stale skill name is never silently dropped from the manifest.
                if allowed_skills is not None:
                    known_skills = registry.names()
                    unknown_skills = sorted(allowed_skills - set(known_skills))
                # The scoped registry is the single permission truth: the tool schema
                # (as_structured_tools iterates it) and the execution-layer fallback
                # (registry.run) read the same object, so an out-of-scope call cannot execute.
                agent_registry = scoped_registry(registry, allowed_skills)
                emit("stage", {"stage": "recall"})
                # Episodic-memory READ side: recall semantically-similar past runs (MemGPT recall
                # storage) + reflections (Reflexion buffer) into the actor's reflection slot — the
                # one existing injection channel. Keyed on the goal, so it applies to a fresh run
                # only: a resume continues a run whose reflections were injected in attempt one.
                embedder = resolve_embedder()
                reflections = (
                    None
                    if resume
                    else episodic.recall_agent_memory(
                        project.sqlite_store, embedder, goal, enabled=recall
                    )
                )
                # T6 HITL form a: arm the gate for a FRESH hitl run; when RESUMING, arm it iff a
                # decision is present (the middleware must be in the resumed graph to receive it).
                # A bare resume stays the T4 crash-continue path (no HITL middleware).
                resume_decisions = hitl_decisions(decision, decision_message) if resume else None
                enable_hitl = (hitl and not resume) or resume_decisions is not None
                interrupt_before = (
                    {s.name for s in agent_registry if s.side_effect is SideEffect.PROPOSES_PATCH}
                    if enable_hitl
                    else None
                )
                emit(
                    "stage",
                    {
                        "stage": "reason",
                        "max_steps": max_steps,
                        "max_model_calls": max_model_calls,
                        "max_cost_usd": max_cost_usd,
                        "max_tool_calls": max_tool_calls,
                    },
                )
                react_agent = ReActAgent(
                    model=model,
                    registry=agent_registry,
                    max_steps=max_steps,
                    max_model_calls=max_model_calls,
                    max_cost_usd=max_cost_usd,
                    max_tool_calls=max_tool_calls,
                    tool_transport=effective_tool_transport,
                    telemetry=telemetry,
                    early_stopping=cast("Literal['force', 'generate']", early_stopping),
                    checkpointer=checkpoint_store.saver,
                    interrupt_before=interrupt_before,
                )
                result = react_agent.run(
                    goal,
                    reflections=reflections,
                    thread_id=effective_thread_id,
                    resume=resume,
                    resume_decisions=resume_decisions,
                )
                emit("stage", {"stage": "persist", "steps": result.step_count})
                # Episodic/recall memory WRITE side, while the project store is still open.
                # Best-effort: a failure logs a WARNING and yields None, so the agent's
                # already-completed result is emitted unchanged either way.
                agent_run_id = persist_agent_run(project.sqlite_store, result)
                # Embed (goal + outcome) so a FUTURE run can recall this one. Skipped on a HITL
                # pause: there is no outcome yet (blank final_answer), so nothing worth embedding.
                if agent_run_id is not None and not result.interrupted:
                    episodic.persist_run_embedding(
                        project.sqlite_store,
                        embedder,
                        run_id=agent_run_id,
                        goal=result.goal,
                        final_answer=result.final_answer,
                    )
                skill_names = agent_registry.names()
            finally:
                tools_mod._shared_project.reset(token)
                telemetry.record_sink = None
    finally:
        checkpoint_store.close()
    telemetry_summary = telemetry.summary()
    cost_budget = summarize_workflow(
        [llm_step("agent_react", telemetry_summary)], budget_usd=react_agent.max_cost_usd
    ).budget
    cumulative_cost = round(react_agent.settled_cost_usd, 6)
    cumulative_limit = react_agent.max_cost_usd
    cost_budget_payload = cost_budget.model_dump(mode="json")
    # ``telemetry`` remains this invocation's raw accounting. ``cost_budget`` is the public run-
    # budget verdict, so on resume its used/remaining/over fields must use the cumulative settled
    # value restored from the checkpoint rather than incorrectly resetting to this invocation's $0.
    cost_budget_payload.update(
        {
            "used_usd": cumulative_cost,
            "remaining_usd": (
                None
                if cumulative_limit is None
                else round(cumulative_limit - cumulative_cost, 6)
            ),
            "over_budget": (
                cumulative_limit is not None and cumulative_cost > cumulative_limit
            ),
            "cost_is_estimate": react_agent.cost_is_estimate,
        }
    )
    payload: dict[str, Any] = {
        **result.model_dump(mode="json"),
        "agent_run_id": agent_run_id,
        # The ACTUAL executable surface for this run (the allow-set), not the full registry.
        "skills": skill_names,
        "llm_mode": llm_mode,
        "tool_transport": effective_tool_transport,
        "mcp_traces": [
            {
                **asdict(trace),
                "argument_summary": dict(trace.argument_summary),
                "risk_flags": list(trace.risk_flags),
            }
            for trace in mcp_traces
        ],
        # The checkpoint thread this run wrote to — pass it back with resume=True to continue an
        # interrupted run. Always present (the checkpointer is always-on).
        "thread_id": effective_thread_id,
        "telemetry": telemetry_summary,
        "cost_budget": cost_budget_payload,
        "run_budget": {
            "planning_turn_limit": react_agent.max_steps,
            "total_model_call_limit": react_agent.max_model_calls,
            "max_cost_usd": react_agent.max_cost_usd,
            "tool_call_limit": react_agent.max_tool_calls,
            "total_model_calls_reserved": react_agent.model_calls_used,
            "model_call_reservations_by_kind": react_agent.model_calls_by_kind,
            "reservation_semantics": "durable_pre_dispatch",
            "settled_cost_usd": cumulative_cost,
            "invocation_cost_usd": telemetry_summary["total_cost_usd"],
            "cumulative_over_budget": (
                cumulative_limit is not None and cumulative_cost > cumulative_limit
            ),
            "cumulative_gate_exhausted": (
                cumulative_limit is not None and cumulative_cost >= cumulative_limit
            ),
            "cost_gate_semantics": "settled_telemetry_pre_dispatch",
            "hard_usd_cap": False,
            "single_call_overshoot_possible": react_agent.max_cost_usd is not None,
            "cost_is_estimate": react_agent.cost_is_estimate,
            "pricing_source": (
                "illustrative_default_or_mixed"
                if react_agent.cost_is_estimate
                else "explicit_config_or_zero"
            ),
        },
        "status": "interrupted" if result.interrupted else "completed",
    }
    if unknown_skills:
        # Only when something was actually unknown: the pair (what you asked for that does not
        # exist, what does exist) is what makes the warning actionable on either front-end.
        payload["unknown_skills"] = unknown_skills
        payload["known_skills"] = known_skills
    return payload
