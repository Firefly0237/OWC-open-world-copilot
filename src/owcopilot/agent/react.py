# 有界 ReAct 循环:LangGraph create_agent 宿主 + 自研 middleware 族(T2 换芯)。
# 要点:① 每步 Observation 是确定性工具的真实输出、非模型臆想 → 结论有 ground truth;
#   ② 动作空间只读/提议、永不写 canon;③ 观测超限截断留标记、上下文超预算读时压缩,都不静默;
#   ④ 循环骨架(模型→工具→模型、tool_calls 回填、退出条件)全部交给标准宿主,
#     自研只保留可辩护资产(截断/压缩/记忆注入/预算收尾/遥测),见 agent/middleware.py。
# T5 plan-and-execute 重规划:单次 run 未成功(耗尽 max_steps / 无接地答案)时,从失败试验蒸馏
#   一条反思(复用 Reflexion 的 distill_reflection),再以该反思指导一次全新 attempt——有界(≤2 轮)
#   且与 Reflexion 组合(反思是新方案的输入)。默认 max_replans=0 关闭,单次运行逐字节等价旧行为;
#   resume(T4 续跑)永不触发重规划,续跑语义不受影响。
# T6 HITL 形态 a(内联 interrupt):敏感动作(默认=PROPOSES_PATCH 技能,agent 唯一写相关动作;它永不
#   写 canon)在执行前用标准 HumanInTheLoopMiddleware 的 interrupt() 暂停,等人类 approve/reject/edit,
#   再以 Command(resume={"decisions":[...]}) 恢复——完全复用 T4 的 checkpointer/thread 基座,不重造。
#   interrupt_before=None(默认)时不挂 HITL middleware,产出与非 HITL run 逐字节一致;暂停不触发 T5
#   重规划(暂停是等人不是失败),续跑(resume)带 decisions 时才是 HITL 恢复、不带则仍是 T4 崩溃续跑。
"""A ReAct agent (Yao et al., 2022) over the OWCopilot skill surface.

The loop skeleton is the standard LangGraph ``create_agent`` host: the model expresses each
action as native ``tool_calls``, the built-in ToolNode executes and back-fills ``ToolMessage``
results, and the loop ends when the model replies without tool calls. What stays self-built are
the defensible assets, carried as middleware (:mod:`owcopilot.agent.middleware`):

* recoverable observation truncation + error-to-observation recovery + serial tool execution,
* read-time context compression (the model sees a compacted view; state is never rewritten),
* Reflexion memory injection as a system-message suffix,
* the force/generate budget finish with the honest ``owc_stop_reason`` tri-state,
* per-call telemetry (:class:`~owcopilot.llm.telemetry_callback.TelemetryCallbackHandler`)
  and the OTEL GenAI span tree.

What makes this more than a demo: every Observation is the output of a *deterministic* tool
(the consistency audit and friends), not the model's own guess. So the agent's conclusions are
grounded in ground truth, and its entire action space is read-only / propose-only — the human
review queue remains the only path that writes canon.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal, cast

from langchain.agents import create_agent
from langchain.agents.middleware import (
    AgentMiddleware,
    HumanInTheLoopMiddleware,
    InterruptOnConfig,
    ModelCallLimitMiddleware,
    ToolCallRequest,
)
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.types import Command
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from langgraph.checkpoint.base import BaseCheckpointSaver

from ..config import CONTEXT_COMPRESSION_KEEP_RECENT, CONTEXT_COMPRESSION_THRESHOLD
from ..llm.otel_bridge import get_tracer_or_noop, invoke_agent_span, trace_id_of_span
from ..llm.telemetry import TelemetryCollector
from ..llm.telemetry_callback import TelemetryCallbackHandler
from ..skills import SkillRegistry
from ..skills.langchain_tools import as_structured_tools, dumps_observation
from ..skills.observation_overflow import bind_overflow
from ..storage.checkpointer import resolve_thread_id
from .middleware import (
    _MAX_STEPS_MESSAGE,
    BudgetFinishMiddleware,
    ContextCompressionMiddleware,
    MemoryInjectionMiddleware,
    ObservationOverflowMiddleware,
    TelemetrySpanMiddleware,
)

logger = logging.getLogger(__name__)

# Cap a single observation fed back into the prompt so a large tool result can't blow the context
# budget. The primary cap is measured in TOKENS (tiktoken cl100k) — the SAME unit as the
# transcript budget (see ContextCompressionMiddleware / context_compressor) — so a CJK-heavy
# result is not under-counted the way a char cap would: 4000 chars is ~1500 tokens of dense JSON
# but ~2000–4000 tokens of CJK. ~2k tokens comfortably fits a typical tool result (an audit of a
# small/medium world — the quality_harness audit measures ~1.3k tokens); genuinely huge outputs
# are truncated with an explicit marker (the model sees it), never silently.
#
# A char ceiling (_OBS_CHAR_CAP_MULT × the token limit = 8000 chars) is layered on as a second
# gate: it catches token-cheap-but-char-huge dumps (e.g. a long run of one repeated byte,
# ~8 chars/token) that slip under the token cap yet would still bloat the prompt. For ordinary
# English/JSON the two gates bind at roughly the same point; for CJK the token gate binds first
# (the T5 goal), and for repeated-byte blobs the char gate binds first.
_OBSERVATION_TOKEN_LIMIT = 2000
_OBS_CHAR_CAP_MULT = 4

#: The billing tier label agent-path model calls are recorded under. The retired gateway routed
#: the agent task to the "cheap" tier in every assembly point (CLI/service/MCP); the single-path
#: agent keeps that convention so cost accounting (telemetry.PRICES) is unchanged.
_AGENT_TIER = "cheap"

#: Hard upper bound on T5 replanning rounds (user decision Q5: "重规划上限 2 轮"). ``max_replans``
#: is validated against this at construction so no caller can request an unbounded replan loop —
#: the "有界" (bounded, no infinite loop) guarantee is enforced structurally, not by convention.
_MAX_REPLANS_CAP = 2


class AgentStep(BaseModel):
    """One executed action and the observation it produced (surfaced for transparency)."""

    thought: str = ""
    action: str = ""
    action_input: dict[str, Any] = Field(default_factory=dict)
    observation: str = ""
    # The STRUCTURED tool result for a successful call, before it is JSON-dumped and
    # truncated into ``observation``.  None for error steps and for non-dict results.
    # Consumers that need an exact field (e.g. a verifier reading ``open_errors``) must use
    # this — the ``observation`` string may be truncated by ``_OBSERVATION_TOKEN_LIMIT`` and
    # cannot be relied on for parsing.
    result: dict[str, Any] | None = None
    is_error: bool = False
    # IN-6: real latency/cost from telemetry CallRecords (not hardcoded; 0.0 for $0 skills)
    latency_ms: float = 0.0
    cost_usd: float = 0.0
    # Observation metadata after (possible) truncation
    observation_chars: int = 0  # len(observation) after truncation
    is_truncated: bool = False  # True iff observation was truncated by truncate_observation()


class AgentResult(BaseModel):
    goal: str
    final_answer: str
    # "finished": model emitted a final answer. "max_steps": ran out of step budget.
    # "max_steps_generate": budget exhausted + the opt-in generate finish produced the answer.
    # "interrupted": the run SUSPENDED at a human gate (T6 HITL form a) and is awaiting a decision.
    stop_reason: str
    steps: list[AgentStep] = Field(default_factory=list)
    step_count: int = 0
    # Context compression accounting (T4-A)
    context_compressions: int = 0  # number of compression rounds triggered
    pre_compress_tokens: int = 0  # transcript tokens before first compression
    post_compress_tokens: int = 0  # transcript tokens after last compression
    # T5 plan-and-execute: how many REPLAN attempts ran AFTER the first (0 ⇒ the first attempt
    # succeeded, or replanning is disabled — the byte-identical default). Bounded by
    # ReActAgent.max_replans (cap _MAX_REPLANS_CAP). This is the result of the LAST attempt run.
    replans: int = 0
    # T6 HITL form a (inline interrupt): True when the run SUSPENDED at a human approval gate
    # instead of finishing. ``interrupt_request`` then carries the standard HITLRequest
    # (``action_requests`` = what the agent wants to do, ``review_configs`` = which decisions are
    # allowed). Resume via ``run(..., resume=True, resume_decisions=[{"type": "approve"}, ...])``.
    # Both default to the non-HITL values so a run without HITL dumps byte-identically (A2).
    interrupted: bool = False
    interrupt_request: dict[str, Any] | None = None


_NATIVE_SYSTEM_PROMPT = """You are a world-consistency assistant for an open-world game project. \
Your job is to diagnose the project's content with the available tools and report what is wrong \
and the safe next step. You never write canon: any fix you find goes to a human review queue.

Use the provided tools (function-calling) to investigate. Call one or more tools per turn; the \
system runs each and returns its real result as a tool message. When you have enough to report, \
reply with a final text answer (no tool calls): a concise report for the human stating what is \
wrong and the safe next step. Never fabricate tool output.

Recovery — when a tool message has status error, read its error_code and adapt: NOT_FOUND means \
the named target or tool is unavailable, so try a different tool or corrected arguments; \
INVALID_ARG means fix the arguments as the message says and retry the same tool; EMPTY_RESULT \
means nothing matched, so broaden or change the query, or conclude if you already have enough; \
CONFLICT means the action is not allowed in this state, so do not repeat it — change approach or \
report; BUDGET_EXCEEDED means stop calling tools and give your final answer now from what you \
have."""


def _model_id_of(model: BaseChatModel) -> str:
    """Best-effort model id of a LangChain chat model (``""`` for fakes that carry none).

    The standard integrations expose ``model_name`` (ChatOpenAI/ChatDeepSeek); some expose
    ``model``. Used for telemetry (`CallRecord.model`) and the gen_ai.request.model span
    attribute — never guessed when the fake reports nothing.
    """
    for attr in ("model_name", "model"):
        value = getattr(model, attr, None)
        if isinstance(value, str) and value:
            return value
    return ""


class _RegistryFallbackMiddleware(AgentMiddleware):
    """Execution-layer skill dispatch for tool calls ``create_agent`` has no bound tool for.

    When the model emits a call whose name is not among the bound tools (an unknown name, or a
    skill the caller's registry does not expose), ``create_agent`` leaves ``request.tool`` unset;
    this middleware dispatches that call through the SAME registry the agent was constructed with
    (``self.registry``), so the execution layer is bounded by exactly that registry — never a
    wider one. The security scope therefore lives in the registry handed to :class:`ReActAgent`:
    the CLI (``--skills``) and the multi-agent workers/verifier pass a
    :func:`~owcopilot.skills.scoped_registry`, so schema and execution share one scope and an
    out-of-scope call is simply unknown to this registry — ``registry.run`` raises the registry's
    own :class:`~owcopilot.skills.SkillError` (also for a genuinely unknown name or the
    ``WRITES_CANON`` auto-invocation ban). That error is caught by the outer
    :class:`~owcopilot.agent.middleware.ObservationOverflowMiddleware` and fed back as a
    recoverable ``Error: ...`` observation — never LangChain's generic "not a valid tool" text and
    never a silent dangling call.
    """

    def __init__(self, registry: SkillRegistry) -> None:
        super().__init__()
        self.registry = registry

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        if request.tool is not None:
            return handler(request)
        name = str(request.tool_call.get("name") or "")
        args = request.tool_call.get("args")
        # SkillError/exceptions propagate to ObservationOverflowMiddleware's recovery net.
        result = self.registry.run(name, dict(args) if isinstance(args, dict) else {})
        return ToolMessage(
            content=dumps_observation(result),
            artifact=result if isinstance(result, dict) else None,
            tool_call_id=str(request.tool_call.get("id") or ""),
            name=name or None,
        )


class ReActAgent:
    """Drives the ReAct loop against a LangChain chat model and a :class:`SkillRegistry`."""

    def __init__(
        self,
        *,
        model: BaseChatModel,
        registry: SkillRegistry,
        max_steps: int = 6,
        task: str = "agent_react",
        # Per-call cost/latency accounting: when supplied, every model call in the run (planning,
        # compaction, the generate finish) appends one CallRecord to this collector via the
        # standard callback surface, and per-step cost/latency are attributed from it.
        telemetry: TelemetryCollector | None = None,
        # T5: observations are capped in TOKENS (same unit as the transcript budget). The char
        # limit is kept for back-compat as a secondary safety ceiling: pass it explicitly to bind
        # a char cap directly, else it is derived from the token limit (_OBS_CHAR_CAP_MULT ×).
        observation_token_limit: int = _OBSERVATION_TOKEN_LIMIT,
        observation_char_limit: int | None = None,
        allowed_skills: set[str] | None = None,  # None = full registry surface (compat)
        # T4-C: transcript_token_budget is the canonical param (tiktoken units).
        # transcript_char_budget is kept for backward compatibility: if only char_budget is
        # supplied it is converted to tokens via ÷4.
        transcript_token_budget: int | None = None,
        transcript_char_budget: int | None = 20_000,  # None = no limit
        # T4-A: context compressor threshold (fraction of token_budget).
        compression_threshold: float = CONTEXT_COMPRESSION_THRESHOLD,
        compression_keep_recent: int = CONTEXT_COMPRESSION_KEEP_RECENT,
        agent_name: str = "world-consistency",  # T4-B: OTEL agent.name attribute
        # Step-budget exhaustion behaviour (opt-in). "force" (default) stops immediately with
        # stop_reason="max_steps" and the canned _MAX_STEPS_MESSAGE, no extra LLM call.
        # "generate" (LangChain AgentExecutor's early_stopping_method="generate") spends ONE
        # extra call asking the model for a real final answer from the transcript collected so
        # far; see BudgetFinishMiddleware for the exact contract and its honest fallback.
        early_stopping: Literal["force", "generate"] = "force",
        # T5 plan-and-execute replanning (user decision Q5): the MAX number of extra "revise the
        # approach and try again" attempts to spend when a run ends UNSUCCESSFULLY (see
        # _run_succeeded). Default 0 ⇒ replanning is OFF and a run is byte-identical to the pre-T5
        # loop (a single attempt). Capped at _MAX_REPLANS_CAP=2 (bounded, no infinite loop).
        # Composes WITH Reflexion, not instead of it: each failed attempt distils ONE reflection
        # reflexion.distill_reflection) that conditions the next attempt via MemoryInjection.
        max_replans: int = 0,
        # T4 resume: a LangGraph checkpointer that persists the run's execution-state channels so a
        # crashed/interrupted run can continue from its last superstep. Default None ⇒ NO
        # checkpointer is attached, the config carries no thread_id, and the run is byte-identical
        # to the pre-T4 behaviour. The assembly layer (CLI) injects a SqliteSaver bound to an
        # independent checkpoints.sqlite; ReActAgent never opens a connection itself.
        checkpointer: BaseCheckpointSaver | None = None,
        # T6 HITL form a (inline interrupt): tool names whose calls PAUSE for human approval before
        # executing, via the standard ``HumanInTheLoopMiddleware`` + LangGraph ``interrupt()`` on
        # T4 checkpointer. Default None ⇒ no HITL middleware is added and the run is byte-identical
        # to a non-HITL run (A2). Requires a ``checkpointer`` (interrupt/resume needs persisted
        # state); the assembly layer (CLI ``--hitl``) passes the registry's PROPOSES_PATCH skills.
        interrupt_before: set[str] | None = None,
    ) -> None:
        if max_steps < 1:
            raise ValueError("max_steps must be >= 1")
        if early_stopping not in ("force", "generate"):
            raise ValueError(
                f"early_stopping must be 'force' or 'generate', got {early_stopping!r}"
            )
        if not 0 <= max_replans <= _MAX_REPLANS_CAP:
            raise ValueError(
                f"max_replans must be between 0 and {_MAX_REPLANS_CAP} (bounded replanning), "
                f"got {max_replans}"
            )
        self.model = model
        self.registry = registry
        self.max_steps = max_steps
        self.task = task
        self.telemetry = telemetry
        self.observation_token_limit = observation_token_limit
        self.observation_char_limit = (
            observation_char_limit
            if observation_char_limit is not None
            else observation_token_limit * _OBS_CHAR_CAP_MULT
        )
        self.allowed_skills = allowed_skills
        # T4-C: resolve token budget — prefer explicit token budget, fall back to char÷4.
        if transcript_token_budget is not None:
            self.transcript_token_budget: int | None = transcript_token_budget
        elif transcript_char_budget is not None:
            self.transcript_token_budget = max(1, transcript_char_budget // 4)
        else:
            self.transcript_token_budget = None  # no limit
        # Keep original char_budget for callers that inspect it directly (backward compat).
        self.transcript_char_budget = transcript_char_budget
        self.compression_threshold = compression_threshold
        self.compression_keep_recent = compression_keep_recent
        self.agent_name = agent_name
        self.early_stopping = early_stopping
        self.max_replans = max_replans
        self.checkpointer = checkpointer
        if interrupt_before and checkpointer is None:
            raise ValueError(
                "interrupt_before(HITL 形态 a)需要一个 checkpointer:interrupt/resume 依赖持久化"
                "的执行态。请通过装配层注入 checkpointer,或不要设置 interrupt_before。"
            )
        self.interrupt_before = set(interrupt_before) if interrupt_before else None
        # The thread_id the LAST run actually used (auto-minted when a checkpointer is attached and
        # none was passed) — the assembly layer surfaces it so a caller knows what id to resume.
        self.last_thread_id: str | None = None
        # Run-scoped recoverable-observation store: obs_id -> the FULL untruncated observation
        # text. Reset at the start of every run (no cross-run leak) and bound to the
        # read_observation skill via a ContextVar for the run's duration. Persists on the agent
        # after a run so a caller can inspect what was truncated.
        self._overflow: dict[str, str] = {}

    def run(
        self,
        goal: str = "",
        reflections: list[str] | None = None,
        *,
        thread_id: str | None = None,
        resume: bool = False,
        resume_decisions: list[dict[str, Any]] | None = None,
    ) -> AgentResult:
        """Run the agent for one goal and return the :class:`AgentResult` of the final attempt.

        The default path is exactly ONE bounded ReAct attempt (:meth:`_run_once`). With
        ``max_replans > 0`` (T5 plan-and-execute, user decision Q5) an attempt that ends
        *unsuccessfully* (see :func:`_run_succeeded` — a budget-exhausted or answerless run)
        triggers a bounded **replan**: a reflection distilled from the failed attempt (reusing
        :func:`~owcopilot.agent.reflexion.distill_reflection`) conditions a fresh attempt, up to
        ``max_replans`` times. See :meth:`_run_with_replanning` for the loop.

        ``reflections`` are recalled Reflexion/episodic notes injected into the FIRST attempt; the
        replan loop accumulates its own distilled reflections on top of them (composition, not
        replacement). ``thread_id`` / ``resume`` are the T4 checkpointer knobs, forwarded to the
        first attempt.

        Two invariants pin the default behaviour:

        * ``max_replans == 0`` ⇒ a single :meth:`_run_once` with the caller's exact arguments, so
          the produced ``AgentResult`` is byte-identical to the pre-T5 loop (A2).
        * ``resume`` ⇒ NEVER replans. A resume continues one interrupted attempt from its checkpoint
          (T4); replanning is about starting *fresh* attempts, a different operation. So a resume
          takes the single-attempt path unchanged, and T4 resume semantics are untouched (A1).

        ``resume_decisions`` (T6 HITL form a) are the human decisions for a run that SUSPENDED at an
        approval gate — one ``{"type": "approve"|"reject"|"edit"|"respond", ...}`` per pending
        action (a single decision is replicated across a multi-action gate). Supplied only with
        ``resume=True``: with them the resume feeds ``Command(resume=...)`` (continue the gate);
        without them a ``resume`` is the T4 crash-continue (``invoke(None)``), byte-identical.
        """
        if resume or self.max_replans == 0:
            return self._run_once(
                goal, reflections, thread_id=thread_id, resume=resume,
                resume_decisions=resume_decisions,
            )
        return self._run_with_replanning(goal, reflections, thread_id=thread_id)

    def _run_with_replanning(
        self, goal: str, reflections: list[str] | None, *, thread_id: str | None
    ) -> AgentResult:
        """Bounded plan-and-execute replanning (T5), composed with Reflexion (Shinn et al., 2023).

        Runs the first attempt, then while the run is unsuccessful and replan budget remains:
        distil ONE reflection from the failed attempt's honest feedback (:func:`_replan_feedback` →
        :func:`~owcopilot.agent.reflexion.distill_reflection` — the SAME Self-Reflection component
        the multi-agent retry uses), APPEND it to the reflection set, and re-run a fresh attempt
        conditioned on the full set. "反思是新方案的输入": the distilled reflection reaches the next
        attempt through :class:`~owcopilot.agent.middleware.MemoryInjectionMiddleware`, exactly as a
        recalled reflection would.

        Boundedness: the loop runs at most ``self.max_replans`` (≤ ``_MAX_REPLANS_CAP`` = 2) extra
        attempts, so it can never spin indefinitely. A blank/failed self-reflection
        (:class:`~owcopilot.agent.reflexion.ReflexionError`) stops replanning and returns the last
        REAL result rather than fabricating a reflection — no silent downgrade.

        Attempt threading: the first attempt uses the caller's ``thread_id`` (may be pinned); every
        replan is a FRESH attempt, so it passes ``thread_id=None`` — with a checkpointer attached a
        new thread is minted, because reusing the failed attempt's thread would *continue* it
        (append to its transcript) rather than restart. ``self.last_thread_id`` therefore ends up
        pointing at the final attempt, the one a caller would resume.
        """
        # Local import breaks the react ↔ reflexion module cycle (reflexion imports from react at
        # module load); distill_reflection is only needed on the failing replan path anyway.
        from .reflexion import ReflexionError, distill_reflection

        # Start from the recalled reflections so attempt 0 sees exactly what a non-replanning run
        # would (byte-identical first attempt when it succeeds); the replan loop appends to this.
        accumulated: list[str] = list(reflections) if reflections else []
        result = self._run_once(
            goal, accumulated or None, thread_id=thread_id, resume=False
        )
        replans = 0
        # An interrupted attempt (T6 HITL form a) is PAUSED awaiting a human decision, not a failed
        # attempt — never replan over it; return it so the human can respond and resume.
        while (
            replans < self.max_replans
            and not _run_succeeded(result)
            and not result.interrupted
        ):
            try:
                reflection = distill_reflection(
                    agent=self,
                    goal=goal,
                    trial_index=replans,
                    feedback=_replan_feedback(result),
                )
            except ReflexionError as exc:
                # Cannot distil a real reflection ⇒ stop replanning and keep the last real result,
                # rather than replanning on an empty reflection (no silent downgrade).
                logger.warning(
                    "T5 replanning stopped after %d attempt(s): self-reflection failed (%s); "
                    "returning the last real result",
                    replans,
                    exc,
                )
                break
            accumulated.append(reflection)
            replans += 1
            result = self._run_once(goal, list(accumulated), thread_id=None, resume=False)
        result.replans = replans
        return result

    def _run_once(
        self,
        goal: str = "",
        reflections: list[str] | None = None,
        *,
        thread_id: str | None = None,
        resume: bool = False,
        resume_decisions: list[dict[str, Any]] | None = None,
    ) -> AgentResult:
        """Run one bounded ReAct loop and assemble the :class:`AgentResult`.

        ``reflections`` is the Reflexion (Shinn et al., 2023) episodic memory: verbal notes
        distilled from PRIOR failed attempts at this same goal, injected as a system-message
        suffix by :class:`~owcopilot.agent.middleware.MemoryInjectionMiddleware`. Default
        ``None`` ⇒ the request reaching the model is byte-identical to a run without memory.

        The graph is assembled fresh per run (compilation is milliseconds) so every middleware
        instance — overflow store, compression cache, step counters, stop reason — is naturally
        run-scoped with zero cross-run leakage.

        Resume (T4, superset signature): with a checkpointer attached, ``resume=True`` +
        ``thread_id`` continues a crashed/interrupted run from its last superstep via
        ``invoke(None)`` — the persisted branches/tools do NOT re-run (LangGraph replays their
        pending writes), and ``goal`` is ignored for execution (recovered from the checkpoint for
        the result label). A fresh run with a checkpointer mints a unique ``thread_id`` when none is
        given; with no checkpointer, passing ``thread_id``/``resume`` is a misuse that fails loudly.
        The default call (no checkpointer, no thread_id) is byte-identical to the pre-T4 loop.

        HITL (T6 form a): when ``self.interrupt_before`` is set, a ``HumanInTheLoopMiddleware``
        guards those tool calls — the model proposes a sensitive call, the graph ``interrupt()``s (
        with ``__interrupt__`` in the state) and this method returns an ``interrupted`` result whose
        ``interrupt_request`` describes the pending gate. A resume carrying ``resume_decisions``
        feeds ``Command(resume={"decisions": ...})`` to continue past the gate (a single decision is
        replicated to cover every pending action); a resume WITHOUT decisions is the T4 crash
        continue (``invoke(None)``), unchanged.

        Known honest boundary: middleware INSTANCE state does not enter the checkpoint (only graph
        channels do). After a resume the overflow store and compression counters start empty/zero,
        so a pre-crash truncated observation's tail is not restorable and
        ``AgentResult.context_compressions`` reflects only the post-resume rounds.
        """
        effective_thread_id = resolve_thread_id(
            self.checkpointer, thread_id=thread_id, resume=resume
        )
        self.last_thread_id = effective_thread_id
        self._overflow = {}
        tracer = get_tracer_or_noop()
        model_id = _model_id_of(self.model)
        compression = ContextCompressionMiddleware(
            chat_model=self.model,
            token_budget=self.transcript_token_budget,
            compression_threshold=self.compression_threshold,
            keep_recent=self.compression_keep_recent,
            tracer=tracer,
            model=model_id,
        )
        # Composition order (first in list = outermost wrap): the gen_ai.chat span wraps the
        # whole model turn including memory/compression view building; the registry fallback is
        # INSIDE ObservationOverflow so its SkillError is caught into an error observation; the
        # stock limiter provides the max_steps semantics; BudgetFinish (after_agent — still runs
        # after the limiter's 'end' jump) resolves owc_stop_reason and the generate finish.
        # list[Any]: the stock/typed middleware carry heterogeneous state-schema generics
        # (BudgetFinishState vs AgentState) that create_agent unifies at runtime.
        middleware: list[Any] = [
            TelemetrySpanMiddleware(model=model_id, tracer=tracer),
            MemoryInjectionMiddleware(reflections),
            compression,
            ObservationOverflowMiddleware(
                overflow=self._overflow,
                token_limit=self.observation_token_limit,
                char_cap=self.observation_char_limit,
                collector=self.telemetry,
                tracer=tracer,
            ),
            _RegistryFallbackMiddleware(self.registry),
        ]
        # T6 HITL form a: a human-approval gate BEFORE the sensitive tool node runs. It hooks
        # ``after_model`` (interrupts once the model has proposed the call, before the ToolNode), so
        # it never enters ObservationOverflow's ``wrap_tool_call`` try/except — its GraphInterrupt
        # bubbles up cleanly to suspend the run. Added only when interrupt_before is set, so the
        # default middleware stack (and produced messages) are byte-identical to a non-HITL run.
        if self.interrupt_before:
            middleware.append(
                HumanInTheLoopMiddleware(
                    interrupt_on={
                        name: InterruptOnConfig(
                            allowed_decisions=["approve", "reject", "edit"]
                        )
                        for name in sorted(self.interrupt_before)
                    }
                )
            )
        middleware += [
            ModelCallLimitMiddleware(run_limit=self.max_steps, exit_behavior="end"),
            BudgetFinishMiddleware(
                chat_model=self.model,
                run_limit=self.max_steps,
                early_stopping=self.early_stopping,
                goal=goal,
            ),
        ]
        agent = create_agent(
            self.model,
            tools=as_structured_tools(self.registry, self.allowed_skills),
            system_prompt=_NATIVE_SYSTEM_PROMPT,
            middleware=middleware,
            # None ⇒ the create_agent default (no checkpointer): no state persistence, no thread_id
            # requirement, produced messages byte-identical to the pre-T4 loop.
            checkpointer=self.checkpointer,
        )
        config: RunnableConfig = {}
        if self.telemetry is not None:
            config["callbacks"] = [
                TelemetryCallbackHandler(
                    self.telemetry, tier=_AGENT_TIER, model=model_id, default_task=self.task
                )
            ]
        if effective_thread_id is not None:
            config["configurable"] = {"thread_id": effective_thread_id}
        # Fresh run seeds the goal message. A resume WITH HITL decisions feeds Command(resume=...)
        # to continue past the approval gate (T6 form a); a resume WITHOUT decisions is the T4 crash
        # continue (invoke(None), byte-identical). A checkpointer + unique thread_id leaves the
        # seeded run byte-identical.
        invoke_input: Any
        if resume:
            invoke_input = (
                Command(resume={"decisions": _sized_decisions(agent, config, resume_decisions)})
                if resume_decisions is not None
                else None
            )
        else:
            invoke_input = {"messages": [HumanMessage(content=f"Goal: {goal}")]}
        # Bind the overflow store for the run's duration so the read_observation skill
        # (dispatched through the registry inside the loop, on LangGraph worker threads —
        # the ContextVar crosses via copy_context) restores tails this run truncated.
        with bind_overflow(self._overflow):
            with invoke_agent_span(tracer, agent_name=self.agent_name, goal=goal) as root_span:
                # Back-fill agent.run_id from the just-started root span's trace_id. The SQLite
                # exporter stores run_id == trace_id, so this makes the documented agent.run_id
                # attribute actually present on the production span.
                run_id = trace_id_of_span(root_span)
                if run_id:
                    try:
                        root_span.set_attribute("agent.run_id", run_id)
                    except Exception:
                        pass  # no-op span when OTEL disabled; never raise here
                state = agent.invoke(cast("Any", invoke_input), config=config)
                # T6 HITL form a: a suspended run returns with __interrupt__ in the state (the
                # ToolNode never ran, so BudgetFinish's owc_stop_reason is absent). Detect it FIRST
                # and surface the pending gate; only a NON-interrupted run reads owc_stop_reason.
                interrupt_request = _interrupt_request_of(state)
                stop_reason = (
                    "interrupted"
                    if interrupt_request is not None
                    else str(state.get("owc_stop_reason") or "finished")
                )
                root_span.set_attribute("agent.stop_reason", stop_reason)
        messages = list(state["messages"])
        # On resume the caller need not re-supply the goal — recover it from the persisted first
        # "Goal: …" message so AgentResult.goal is accurate.
        result_goal = goal or (_goal_from_messages(messages) if resume else goal)
        return _assemble_result(
            goal=result_goal,
            messages=messages,
            stop_reason=stop_reason,
            compression=compression,
            interrupt_request=interrupt_request,
        )


def _run_succeeded(result: AgentResult) -> bool:
    """Whether a completed run counts as a SUCCESS for replanning (T5).

    The single success is a model that finished on its own terms with a non-empty answer
    (``stop_reason == "finished"``). The unsuccessful signals are the REAL ones
    :class:`~owcopilot.agent.middleware.BudgetFinishMiddleware` emits — no fabricated
    refusal/grounding classifier:

    * ``max_steps`` — the step budget was exhausted; ``final_answer`` is the canned notice.
    * ``max_steps_generate`` — a best-effort answer was forced out of the transcript under the same
      exhaustion (``early_stopping="generate"``); still "did not finish the investigation".
    * ``finished`` with a blank ``final_answer`` — the model stopped but produced no grounded text.

    All three are treated as unsuccessful so a bounded replan may improve on them.
    """
    return result.stop_reason == "finished" and bool(result.final_answer.strip())


def _replan_feedback(result: AgentResult) -> str:
    """Honest, self-contained account of why an attempt was unsuccessful — the evaluator feedback
    handed to the Self-Reflection distiller (:func:`~owcopilot.agent.reflexion.distill_reflection`)
    before a replan. Built only from real ``AgentResult`` fields, never a guessed diagnosis."""
    error_steps = sum(1 for step in result.steps if step.is_error)
    if result.stop_reason in ("max_steps", "max_steps_generate"):
        reason = (
            f"exhausted the {result.step_count}-step budget without reaching a grounded "
            "final answer"
        )
    elif not result.final_answer.strip():
        reason = "finished without producing any final answer"
    else:
        reason = f"ended unsuccessfully (stop_reason={result.stop_reason})"
    errors = f"; {error_steps} of {result.step_count} tool step(s) errored" if error_steps else ""
    return (
        f"The previous attempt {reason}{errors}. Revise the approach: choose fewer, better-"
        "targeted tool calls and reach a grounded conclusion within the step budget."
    )


def _interrupt_request_of(state: dict[str, Any]) -> dict[str, Any] | None:
    """The pending HITL request when the run SUSPENDED at a human gate (T6 form a), else ``None``.

    ``create_agent``'s ``invoke`` returns the graph state with an ``__interrupt__`` entry — a list
    of LangGraph ``Interrupt`` objects — whenever a middleware ``interrupt()`` suspended the run.
    The first interrupt's ``.value`` is the standard ``HITLRequest`` (``action_requests`` = what the
    agent wants to do, ``review_configs`` = which decisions are allowed); it is surfaced verbatim so
    a caller knows exactly what needs approval. Returns ``None`` when the run completed normally.
    """
    raw = state.get("__interrupt__")
    if not raw:
        return None
    value = getattr(raw[0], "value", None)
    return dict(value) if isinstance(value, dict) else {"value": value}


def _sized_decisions(
    agent: Any, config: RunnableConfig, decisions: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Match a one-decision list to the number of actions in the pending HITL gate.

    A single gate (one AIMessage) can fan out to several sensitive tool calls, and the
    ``HumanInTheLoopMiddleware`` requires exactly one decision per interrupted action. The CLI
    passes ONE ``--decision`` for the whole gate, so a single decision is replicated across pending
    action read from the persisted checkpoint. An already-sized (or empty) list is returned as-is;
    if the pending gate cannot be read, the list is passed through unchanged (the middleware then
    raises its own count-mismatch error rather than this silently guessing).
    """
    if len(decisions) != 1:
        return decisions
    try:
        snapshot = agent.get_state(config)
    except Exception:
        return decisions
    for interrupt_obj in snapshot.interrupts or ():
        value = getattr(interrupt_obj, "value", None)
        if isinstance(value, dict) and value.get("action_requests"):
            count = len(value["action_requests"])
            return decisions * count if count > 1 else decisions
    return decisions


def _goal_from_messages(messages: list[Any]) -> str:
    """Recover the goal from the persisted first ``HumanMessage`` (``"Goal: <goal>"``).

    Used on resume, where the caller may not re-supply the goal: the seed message is in the
    checkpoint. Returns ``""`` if no such message is present (nothing to recover from)."""
    first_human = next((m for m in messages if isinstance(m, HumanMessage)), None)
    if first_human is None:
        return ""
    text = str(first_human.text or "")
    prefix = "Goal: "
    return text[len(prefix) :] if text.startswith(prefix) else text


def _assemble_result(
    *,
    goal: str,
    messages: list[Any],
    stop_reason: str,
    compression: ContextCompressionMiddleware,
    interrupt_request: dict[str, Any] | None = None,
) -> AgentResult:
    """Fold the final graph state into the stable :class:`AgentResult` contract.

    Steps come from (AIMessage tool_calls → matching ToolMessage) pairs: thought is the turn's
    text on the FIRST step only (a turn has one text blob but may fan out several calls);
    observation/result/is_error come from the ToolMessage (content / artifact / status); the
    per-step cost/latency and truncation metadata are read back from the ``owc_*``
    ``response_metadata`` keys the middleware stamped.

    ``stop_reason=="max_steps"`` substitutes the canned :data:`_MAX_STEPS_MESSAGE` for the
    limiter's artificial "Model call limits exceeded" message — that message never leaks into
    ``final_answer`` and never becomes a step (it carries no tool_calls).

    ``interrupt_request`` (T6 HITL form a) marks a run SUSPENDED at a human gate: the pending
    sensitive call has no ToolMessage yet (so it produces no step above), ``final_answer`` is
    blank (there is no answer yet — the last AIMessage is only the proposal), and the pending gate
    is carried on the result for the caller to surface.
    """
    tool_results: dict[str, ToolMessage] = {
        str(m.tool_call_id): m for m in messages if isinstance(m, ToolMessage)
    }
    steps: list[AgentStep] = []
    for message in messages:
        if not isinstance(message, AIMessage) or not message.tool_calls:
            continue
        for i, call in enumerate(message.tool_calls):
            tool_msg = tool_results.get(str(call.get("id") or ""))
            if tool_msg is None:
                continue  # unanswered call (interrupted run) — no observation, no step
            observation = (
                tool_msg.content if isinstance(tool_msg.content, str) else str(tool_msg.content)
            )
            meta = tool_msg.response_metadata
            args = call.get("args")
            steps.append(
                AgentStep(
                    # The assistant turn's text IS the ReAct thought (Yao et al., 2022);
                    # attached to the first step of the turn only, never duplicated.
                    thought=str(message.text or "") if i == 0 else "",
                    action=str(call.get("name") or ""),
                    action_input=dict(args) if isinstance(args, dict) else {},
                    observation=observation,
                    result=tool_msg.artifact if isinstance(tool_msg.artifact, dict) else None,
                    is_error=tool_msg.status == "error",
                    latency_ms=float(meta.get("owc_latency_ms", 0.0)),
                    cost_usd=float(meta.get("owc_cost_usd", 0.0)),
                    observation_chars=int(meta.get("owc_observation_chars", len(observation))),
                    is_truncated=bool(meta.get("owc_is_truncated", False)),
                )
            )
    if interrupt_request is not None:
        final_answer = ""  # suspended at a human gate — no final answer yet
    elif stop_reason == "max_steps":
        final_answer = _MAX_STEPS_MESSAGE
    else:
        last_ai = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
        final_answer = str(last_ai.text or "") if last_ai is not None else ""
    return AgentResult(
        goal=goal,
        final_answer=final_answer,
        stop_reason=stop_reason,
        steps=steps,
        step_count=len(steps),
        context_compressions=compression.context_compressions,
        pre_compress_tokens=compression.pre_compress_tokens,
        post_compress_tokens=compression.post_compress_tokens,
        interrupted=interrupt_request is not None,
        interrupt_request=interrupt_request,
    )
