# 有界 ReAct 循环（Yao 2022 文本式）。默认走原生 function-calling，供应商不支持则回退文本。
# 要点：① 每步 Observation 是确定性工具的真实输出、非模型臆想 → 结论有 ground truth；
#   ② 动作空间只读/提议、永不写 canon；③ 观测超限截断留标记、上下文超预算触发压缩，都不静默。
"""A ReAct agent (Yao et al., 2022) over the OWCopilot skill surface.

This is the *canonical*, text-based ReAct loop — the original paper's form, which predates
function-calling APIs and fits this project's text-in/text-out gateway exactly. At each turn the
model emits::

    Thought: <reasoning>
    Action: <one skill name>
    Action Input: <a dict of arguments>

the loop executes that skill, appends an ``Observation:`` with the real result, and repeats until
the model emits ``Final Answer:`` or the step budget runs out.

What makes this more than a demo: every Observation is the output of a *deterministic* tool
(the consistency audit and friends), not the model's own guess. So the agent's conclusions are
grounded in ground truth, and its entire action space is read-only / propose-only — the human
review queue remains the only path that writes canon.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..llm.gateway import LLMGateway
from ..llm.jsonio import extract_json_object
from ..llm.otel_bridge import (
    execute_tool_span,
    gen_ai_chat_span,
    get_tracer_or_noop,
    invoke_agent_span,
    trace_id_of_span,
)
from ..llm.tokenizer import count_tokens
from ..skills import SkillError, SkillRegistry
from ..skills.observation_overflow import bind_overflow, truncate_observation

logger = logging.getLogger(__name__)

# Cap a single observation fed back into the prompt so a large tool result can't blow the context
# budget. The primary cap is measured in TOKENS (tiktoken cl100k) — the SAME unit as react's
# transcript budget (see _trim_transcript / _compress_view / context_compressor) — so a CJK-heavy
# result is not under-counted the way a char cap would: 4000 chars is ~1500 tokens of dense JSON but
# ~2000–4000 tokens of CJK. ~2k tokens comfortably fits a typical tool result (an audit of a
# small/medium world — the quality_harness audit measures ~1.3k tokens); genuinely huge outputs are
# truncated with an explicit marker (the model sees it), never silently.
#
# A char ceiling (_OBS_CHAR_CAP_MULT × the token limit = 8000 chars) is layered on as a second gate:
# it catches token-cheap-but-char-huge dumps (e.g. a long run of one repeated byte, ~8 chars/token)
# that slip under the token cap yet would still bloat the prompt. For ordinary English/JSON the two
# gates bind at roughly the same point; for CJK the token gate binds first (the T5 goal), and for
# repeated-byte blobs the char gate binds first.
_OBSERVATION_TOKEN_LIMIT = 2000
_OBS_CHAR_CAP_MULT = 4

# Placeholder for gen_ai.request.model until the gateway resolves the real model behind the tier;
# back-filled from telemetry after each planning call (see _update_chat_span_from_telemetry).
_PENDING_MODEL = "(resolving)"

# The step-budget-exhausted message for the default `early_stopping="force"` behaviour (unchanged
# since before Reflexion/native-tools existed) and the honest fallback wording when
# `early_stopping="generate"`'s extra finish call itself fails (see _budget_exhausted_answer).
_MAX_STEPS_MESSAGE = (
    "Reached the step budget before finishing. "
    "Latest findings are in the steps above; "
    "re-run with a higher --max-steps to continue."
)

# System prompt for the ONE extra "generate" finish call (LangChain AgentExecutor's
# early_stopping_method="generate"): explicitly forbids another tool call and asks the model to
# answer from the transcript alone, or admit it cannot and summarize what it has. Kept generic (no
# per-goal specifics) so it never overfits one call site.
_GENERATE_FINISH_SYSTEM = (
    "You are a world-consistency assistant for an open-world game project. The tool-call step "
    "budget for this investigation is exhausted — you may NOT call another tool and no Action will "
    "be executed even if you write one. Based ONLY on the goal and the transcript of Thoughts/"
    "Actions/Observations below, give your Final Answer now: what you found, and the safe next "
    "step. If the transcript does not have enough to fully answer, say so explicitly and summarize "
    "what you do know. Reply in the form:\n\nThought: <your reasoning>\nFinal Answer: <your answer>"
)


class ParsedStep(BaseModel):
    """One parsed model turn: either an action to take, or a final answer."""

    thought: str = ""
    action: str | None = None
    action_input: dict[str, Any] = Field(default_factory=dict)
    final_answer: str | None = None


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
    # IN-6: real latency/cost from gateway CallRecords (not hardcoded; 0.0 only for mock tier)
    latency_ms: float = 0.0
    cost_usd: float = 0.0
    # Observation metadata after (possible) truncation
    observation_chars: int = 0  # len(observation) after truncation
    is_truncated: bool = False  # True iff observation was truncated by truncate_observation()


class AgentResult(BaseModel):
    goal: str
    final_answer: str
    # "finished": model emitted a Final Answer. "max_steps": ran out of step budget.
    stop_reason: str
    steps: list[AgentStep] = Field(default_factory=list)
    step_count: int = 0
    # Context compression accounting (T4-A)
    context_compressions: int = 0  # number of compression rounds triggered
    pre_compress_tokens: int = 0  # transcript tokens before first compression
    post_compress_tokens: int = 0  # transcript tokens after last compression


_SYSTEM_PROMPT = """You are a world-consistency assistant for an open-world game project. Your job \
is to diagnose the project's content with the available tools and report what is wrong and the \
safe next step. You never write canon: any fix you find goes to a human review queue.

You work in a strict ReAct loop. On each turn output EXACTLY one of these two shapes:

Thought: <your reasoning about what to check next>
Action: <exactly one tool name from the list below>
Action Input: <a dict of arguments for that tool, e.g. {{"query": "main quest giver"}}; use {{}} \
if none>

or, once you have enough to report:

Thought: <your reasoning>
Final Answer: <a concise report for the human: what is wrong, and the safe next step>

Rules:
- Output ONE Thought and ONE Action (or ONE Final Answer). Stop after it.
- Never write an "Observation:" line yourself — the system runs the tool and appends the real \
result. Never invent tool output.
- Use only tool names from this list. Arguments must match the tool's parameters \
(a trailing * marks a required argument).

Available tools:
{manifest}"""


class ReActAgent:
    """Drives the ReAct loop against an :class:`LLMGateway` and a :class:`SkillRegistry`."""

    def __init__(
        self,
        *,
        gateway: LLMGateway,
        registry: SkillRegistry,
        max_steps: int = 6,
        task: str = "agent_react",
        # T5: observations are capped in TOKENS (same unit as the transcript budget). The char
        # limit is kept for back-compat as a secondary safety ceiling: pass it explicitly to bind a
        # char cap directly, else it is derived from the token limit (_OBS_CHAR_CAP_MULT ×).
        observation_token_limit: int = _OBSERVATION_TOKEN_LIMIT,
        observation_char_limit: int | None = None,
        allowed_skills: set[str] | None = None,  # None = full manifest (compat)
        # T4-C: transcript_token_budget is the canonical param (tiktoken units).
        # transcript_char_budget is kept for backward compatibility: if only char_budget is
        # supplied it is converted to tokens via ÷4 with a deprecation note in docstring.
        transcript_token_budget: int | None = None,
        transcript_char_budget: int | None = 20_000,  # None = no limit
        # T4-A: context compressor threshold (fraction of token_budget).
        compression_threshold: float = 0.70,
        compression_keep_recent: int = 3,
        agent_name: str = "world-consistency",  # T4-B: OTEL agent.name attribute
        # Native OpenAI function-calling vs the text ReAct format. Default True = *prefer native*:
        # the agent uses the provider's native tool-calling when the routed provider supports it
        # (provider_supports_tools probe) and transparently falls back to the text ReAct loop when
        # it does not, so providers without native tool support keep using the text path unchanged.
        # Set False to force the text format.
        use_native_tools: bool = True,
        # Step-budget exhaustion behaviour (opt-in). "force" (default) is byte-identical to the
        # pre-existing behaviour: stop immediately with stop_reason="max_steps" and the canned
        # _MAX_STEPS_MESSAGE, no extra LLM call. "generate" (LangChain AgentExecutor's
        # early_stopping_method="generate") spends ONE extra planning call asking the model to
        # produce a real final answer from the transcript collected so far, instead of discarding
        # it; see _budget_exhausted_answer for the exact contract and its honest fallback.
        early_stopping: Literal["force", "generate"] = "force",
    ) -> None:
        if max_steps < 1:
            raise ValueError("max_steps must be >= 1")
        if early_stopping not in ("force", "generate"):
            raise ValueError(
                f"early_stopping must be 'force' or 'generate', got {early_stopping!r}"
            )
        self.gateway = gateway
        self.registry = registry
        self.max_steps = max_steps
        self.task = task
        # T5: the primary observation cap is in tokens (count_tokens, cl100k) — chosen over
        # retrieval/budget.bge-m3 so the observation cap uses the SAME unit as the transcript budget
        # and stays offline / no-transformers. observation_char_limit stays an int attribute for
        # back-compat: an explicitly-passed value is honored as the char cap, else it is derived
        # from the token limit as a pathological-expansion ceiling.
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
            # Backward-compat shim: convert char budget to tokens (rough but safe).
            self.transcript_token_budget = max(1, transcript_char_budget // 4)
        else:
            self.transcript_token_budget = None  # no limit
        # Keep original char_budget for callers that inspect it directly (backward compat).
        self.transcript_char_budget = transcript_char_budget
        self.compression_threshold = compression_threshold
        self.compression_keep_recent = compression_keep_recent
        self.agent_name = agent_name
        self.use_native_tools = use_native_tools
        self.early_stopping = early_stopping
        # Run-scoped recoverable-observation store: obs_id -> the FULL untruncated observation text.
        # Reset at the start of every run (no cross-run leak) and bound to the read_observation
        # skill via a ContextVar for the run's duration. Persists on the agent after a run so a
        # caller can inspect what was truncated.
        self._overflow: dict[str, str] = {}

    def _native_tools_available(self) -> bool:
        """True iff native tool-calling is requested AND the provider the agent's task routes to
        actually supports it. Resolving the provider here keeps the probe honest (it inspects the
        real provider behind the tier, including resilience wrappers, which transparently expose
        their inner ``.complete_with_tools``)."""
        if not self.use_native_tools:
            return False
        try:
            from ..llm.gateway import provider_supports_tools  # noqa: PLC0415

            _tier, provider = self.gateway._resolve_provider(task=self.task, tier=None)
            return provider_supports_tools(provider)
        except Exception:
            return False  # any resolution problem ⇒ fall back to the text path

    def run(self, goal: str, reflections: list[str] | None = None) -> AgentResult:
        # Prefer the native function-calling loop when the routed provider supports it; otherwise
        # the canonical text ReAct loop runs. Both dispatch every tool through the same registry, so
        # argument validation (and the safe action space) is identical on either path.
        #
        # ``reflections`` is the Reflexion (Shinn et al., 2023) episodic memory: verbal notes
        # distilled from PRIOR failed attempts at this same goal, injected as extra context so the
        # actor learns from what went wrong. It is supplied by the multi-agent single-shot retry
        # (:meth:`owcopilot.multi_agent.session.MultiAgentSession._reflexion_retry`), whose
        # Self-Reflection step is :func:`owcopilot.agent.reflexion.distill_reflection`. Default
        # ``None`` ⇒ byte-identical to the pre-Reflexion behaviour on both paths (no reflection
        # block is rendered).
        #
        # Start each run with a fresh overflow store and bind it so the read_observation skill
        # (dispatched through the registry inside either loop) restores tails this run truncated.
        # The bind is reset on exit, so read_observation outside a run gives its guided error.
        self._overflow = {}
        with bind_overflow(self._overflow):
            if self._native_tools_available():
                return self._run_native_tools(goal, reflections)
            return self._run_text(goal, reflections)

    def _run_text(self, goal: str, reflections: list[str] | None = None) -> AgentResult:
        # Use filtered manifest when allowed_skills is set
        system = _SYSTEM_PROMPT.format(manifest=self.registry.manifest(allowed=self.allowed_skills))
        # the running Thought/Action/Observation scratchpad (append-only)
        transcript: list[str] = []
        steps: list[AgentStep] = []

        # T4-B: get tracer (no-op when OWCOPILOT_OTEL_ENABLED != "1")
        tracer = get_tracer_or_noop()

        # T4-A: cumulative compression stats across the run
        total_compressions = 0
        first_pre_tokens = 0
        last_post_tokens = 0

        # T4-A (cost): memoise the compacted prefix across steps so a growing transcript is
        # compressed incrementally (new turns folded into the cached summary) instead of
        # re-summarising the whole head from scratch every step — O(steps) → ~O(1) extra calls.
        from .context_compressor import CompressionCache  # noqa: PLC0415

        compression_cache = CompressionCache()

        with invoke_agent_span(tracer, agent_name=self.agent_name, goal=goal) as root_span:
            # Back-fill agent.run_id from the just-started root span's trace_id. The SQLite
            # exporter stores run_id == trace_id, so this makes the documented agent.run_id
            # attribute actually present on the production span (previously dangling — it was only
            # set when a run_id was passed in, which the single call site never did).
            run_id = trace_id_of_span(root_span)
            if run_id:
                try:
                    root_span.set_attribute("agent.run_id", run_id)
                except Exception:
                    pass  # no-op span when OTEL disabled; never raise here
            for step_idx in range(self.max_steps):
                # T4-A: build read-time view (transcript itself never mutated — append-only)
                view, comp_stats = _compress_view(
                    gateway=self.gateway,
                    transcript=transcript,
                    token_budget=self.transcript_token_budget,
                    compression_threshold=self.compression_threshold,
                    keep_recent=self.compression_keep_recent,
                    cache=compression_cache,
                )
                if comp_stats.triggered:
                    if total_compressions == 0:
                        first_pre_tokens = comp_stats.pre_compress_tokens
                    last_post_tokens = comp_stats.post_compress_tokens
                    total_compressions += comp_stats.context_compressions

                # T4-B: gen_ai.chat span for the planning LLM call.
                # The real model id behind the tier is only known once the gateway resolves the
                # provider, so the span opens with a placeholder and gen_ai.request.model is
                # back-filled below from the telemetry record (NOT self.task, which is an internal
                # task label and would violate the GenAI semantic convention).
                with gen_ai_chat_span(tracer, model=_PENDING_MODEL, step_idx=step_idx) as chat_span:
                    raw = self.gateway.complete(
                        task=self.task,
                        system=system,
                        # T4-C: pass token-budget-compressed view instead of raw transcript.
                        # Reflexion memory (constant across the loop) is rendered ahead of the
                        # transcript by _user_prompt; None/empty ⇒ prompt is byte-identical.
                        user=_user_prompt(
                            goal, view, self.transcript_token_budget, reflections=reflections
                        ),
                    )
                    # Back-fill real model id + token counts from the latest telemetry record.
                    _update_chat_span_from_telemetry(chat_span, self.gateway)

                parsed = parse_react_step(raw)

                if parsed.final_answer is not None:
                    root_span.set_attribute("agent.stop_reason", "finished")
                    return AgentResult(
                        goal=goal,
                        final_answer=parsed.final_answer,
                        stop_reason="finished",
                        steps=steps,
                        step_count=len(steps),
                        context_compressions=total_compressions,
                        pre_compress_tokens=first_pre_tokens,
                        post_compress_tokens=last_post_tokens,
                    )

                if parsed.action is None:
                    # The model produced neither an Action nor a Final Answer. Nudge it once via the
                    # transcript and spend a step rather than crashing.
                    observation = (
                        "No Action or Final Answer found. "
                        "Reply with 'Action:' + 'Action Input:' to "
                        "use a tool, or 'Final Answer:' to finish."
                    )
                    steps.append(
                        AgentStep(
                            thought=parsed.thought,
                            observation=observation,
                            is_error=True,
                            observation_chars=len(observation),
                            is_truncated=False,
                        )
                    )
                    transcript.append(_render_turn(parsed.thought, None, {}, observation))
                    continue

                # IN-6: snapshot telemetry record count before skill execution
                snap_idx = len(self.gateway.telemetry.records)

                # T4-B: execute_tool span wraps the actual skill call.
                # On any exception the span is marked ERROR and the exception is recorded
                # so the trace can distinguish error steps from successful ones.
                with execute_tool_span(
                    tracer, tool_name=parsed.action, step_idx=step_idx
                ) as tool_span:
                    # Structured result captured BEFORE truncation so downstream consumers
                    # (e.g. a verifier reading an exact field) need not parse the possibly
                    # truncated observation string.  None unless the call succeeds with a dict.
                    structured_result: dict[str, Any] | None = None
                    try:
                        result = self.registry.run(parsed.action, parsed.action_input)
                        if isinstance(result, dict):
                            structured_result = result
                        raw_dump = _dumps(result)
                        observation, obs_truncated = truncate_observation(
                            self._overflow,
                            raw_dump,
                            step_idx,
                            token_limit=self.observation_token_limit,
                            char_cap=self.observation_char_limit,
                        )
                        is_error = False
                    except SkillError as exc:
                        observation = f"Error: {exc}"
                        obs_truncated = False  # error string is not truncated
                        is_error = True
                        # Mark span as ERROR so traces can distinguish tool failures
                        try:
                            from opentelemetry.trace import Status, StatusCode  # noqa: PLC0415

                            tool_span.set_status(Status(StatusCode.ERROR, str(exc)))
                            tool_span.record_exception(exc)
                        except Exception:
                            pass  # span may be a no-op (OTEL disabled); never raise here
                    except Exception as exc:  # a tool blew up; report as observation, don't crash
                        observation = f"Error: {type(exc).__name__}: {exc}"
                        obs_truncated = False  # error string is not truncated
                        is_error = True
                        # Mark span as ERROR so traces can distinguish tool failures
                        try:
                            from opentelemetry.trace import Status, StatusCode  # noqa: PLC0415

                            tool_span.set_status(Status(StatusCode.ERROR, str(exc)))
                            tool_span.record_exception(exc)
                        except Exception:
                            pass  # span may be a no-op (OTEL disabled); never raise here

                # IN-6: aggregate real telemetry for this step (honest even on error)
                step_records = self.gateway.telemetry.records_since(snap_idx)
                step_latency_ms = sum(r.latency_ms for r in step_records)
                step_cost_usd = sum(r.cost_usd for r in step_records)

                steps.append(
                    AgentStep(
                        thought=parsed.thought,
                        action=parsed.action,
                        action_input=parsed.action_input,
                        observation=observation,
                        result=structured_result,
                        is_error=is_error,
                        latency_ms=step_latency_ms,
                        cost_usd=step_cost_usd,
                        observation_chars=len(observation),  # len after truncation
                        is_truncated=obs_truncated,
                    )
                )
                transcript.append(
                    _render_turn(parsed.thought, parsed.action, parsed.action_input, observation)
                )

            final_answer, stop_reason = self._budget_exhausted_answer(
                goal=goal, transcript_text="\n\n".join(transcript)
            )
            root_span.set_attribute("agent.stop_reason", stop_reason)
            return AgentResult(
                goal=goal,
                final_answer=final_answer,
                stop_reason=stop_reason,
                steps=steps,
                step_count=len(steps),
                context_compressions=total_compressions,
                pre_compress_tokens=first_pre_tokens,
                post_compress_tokens=last_post_tokens,
            )

    def _run_native_tools(self, goal: str, reflections: list[str] | None = None) -> AgentResult:
        """Structured ReAct loop using the provider's native OpenAI function-calling.

        Behaviourally equivalent to :meth:`_run_text` — same skill registry, same ``AgentStep``
        records, same OTEL span tree (gen_ai.chat → execute_tool), same WRITES_CANON / unknown-skill
        / step-budget safety — but the *model expresses its action* as structured ``tool_calls``
        rather than the ``Action:``/``Action Input:`` text format. The loop:

          1. send the running message history + tool schemas to the gateway,
          2. if the model returns tool_calls, execute each via the registry (reusing
             AgentStep.result and execute_tool_span) and append a ``role=tool`` message per call,
          3. otherwise the model's text is the Final Answer and the loop ends,
          4. iterate to the step budget.
        """
        system = _NATIVE_SYSTEM_PROMPT
        tools = self.registry.openai_tools(allowed=self.allowed_skills)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": f"Goal: {goal}"},
        ]
        # Reflexion episodic memory: one extra user message carrying reflections distilled from
        # prior failed attempts. Appended only when non-empty, so the message list is byte-identical
        # to the pre-Reflexion default (system + "Goal:") when no reflections are supplied. Rendered
        # via the same _reflections_block helper the text path uses, so both paths word it alike.
        if reflections:
            messages.append({"role": "user", "content": _reflections_block(reflections)})
        steps: list[AgentStep] = []
        tracer = get_tracer_or_noop()

        with invoke_agent_span(tracer, agent_name=self.agent_name, goal=goal) as root_span:
            run_id = trace_id_of_span(root_span)
            if run_id:
                try:
                    root_span.set_attribute("agent.run_id", run_id)
                except Exception:
                    pass
            for step_idx in range(self.max_steps):
                with gen_ai_chat_span(tracer, model=_PENDING_MODEL, step_idx=step_idx) as chat_span:
                    resp = self.gateway.complete_with_tools(
                        task=self.task, messages=messages, tools=tools
                    )
                    _update_chat_span_from_telemetry(chat_span, self.gateway)

                if not resp.wants_tool_calls:
                    # No tool calls ⇒ the model's text is the final answer.
                    root_span.set_attribute("agent.stop_reason", "finished")
                    return AgentResult(
                        goal=goal,
                        final_answer=resp.text,
                        stop_reason="finished",
                        steps=steps,
                        step_count=len(steps),
                    )

                # Echo the assistant's tool-call request back into the history (required by the
                # OpenAI contract so the following tool results correlate to these call ids). Per
                # the OpenAI function-calling guide we append the FULL assistant message, so its
                # ``content`` text (the ReAct thought) rides along with the tool_calls.
                messages.append(_assistant_tool_call_message(resp.tool_calls, text=resp.text))

                # The assistant turn has exactly one text blob (the ReAct thought) but may request
                # several tool_calls. Attach that thought to the FIRST step of the turn only; later
                # steps of the same turn keep thought="" so the reasoning is not duplicated. The
                # single-tool-call case (the ReAct norm) gets steps[0].thought == resp.text exactly.
                for i, call in enumerate(resp.tool_calls):
                    thought = resp.text if i == 0 else ""
                    snap_idx = len(self.gateway.telemetry.records)
                    step = self._execute_native_call(tracer, call, step_idx, thought=thought)
                    # IN-6: real per-step latency/cost from telemetry since the snapshot. The
                    # planning call already recorded before this loop body; attribute only the
                    # records since the snapshot (tool execution itself is $0/deterministic here).
                    records = self.gateway.telemetry.records_since(snap_idx)
                    step.latency_ms = sum(r.latency_ms for r in records)
                    step.cost_usd = sum(r.cost_usd for r in records)
                    steps.append(step)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.call_id,
                            "content": step.observation,
                        }
                    )

            final_answer, stop_reason = self._budget_exhausted_answer(
                goal=goal, transcript_text=_render_native_transcript(messages)
            )
            root_span.set_attribute("agent.stop_reason", stop_reason)
            return AgentResult(
                goal=goal,
                final_answer=final_answer,
                stop_reason=stop_reason,
                steps=steps,
                step_count=len(steps),
            )

    def _budget_exhausted_answer(self, *, goal: str, transcript_text: str) -> tuple[str, str]:
        """Resolve the (final_answer, stop_reason) pair once the step budget is exhausted.

        ``early_stopping="force"`` (default): returns the canned :data:`_MAX_STEPS_MESSAGE` and
        ``stop_reason="max_steps"`` — byte-identical to the pre-existing behaviour, no extra call.

        ``early_stopping="generate"``: spends ONE extra ``gateway.complete()`` call — the plain
        text completion, not ``complete_with_tools``, so this works identically whether the run
        used the native-tools loop or the text loop, and regardless of whether the routed provider
        supports native tools — with a system prompt that forbids further tool use and asks for a
        real answer (or an honest "I can't fully answer" + summary) from the transcript alone. On
        success: ``stop_reason="max_steps_generate"`` and the model's own final answer (falling
        back to its raw reply if it didn't use the "Final Answer:" marker, and to the canned
        message only if the reply is empty). If that extra call itself raises, this falls back to
        the exact "force" wording/stop_reason and logs a WARNING — never crashes, never silently
        drops the run's findings.
        """
        if self.early_stopping != "generate":
            return _MAX_STEPS_MESSAGE, "max_steps"
        user = (
            f"Goal: {goal}\n\n{transcript_text}\n\n"
            "The step budget above is exhausted. Do not propose another action."
        )
        try:
            raw = self.gateway.complete(task=self.task, system=_GENERATE_FINISH_SYSTEM, user=user)
        except Exception:
            logger.warning(
                "ReActAgent: early_stopping='generate' finish call failed; "
                "falling back to the 'force' stop.",
                exc_info=True,
            )
            return _MAX_STEPS_MESSAGE, "max_steps"
        final_answer = parse_react_step(raw).final_answer or raw.strip() or _MAX_STEPS_MESSAGE
        return final_answer, "max_steps_generate"

    def _execute_native_call(
        self, tracer: Any, call: Any, step_idx: int, *, thought: str = ""
    ) -> AgentStep:
        """Execute one native tool call via the registry and build its :class:`AgentStep`.

        Shares the registry dispatch, structured-result capture, observation truncation and
        execute_tool span with the text path — only the *source* of (name, args) differs (a parsed
        ``ToolCall`` instead of a text-parsed action). Returns the step with latency/cost left at
        0.0 for the caller to back-fill from telemetry; ``thought`` is supplied by the caller (the
        assistant turn's text, which IS the ReAct reasoning trace).
        """
        structured_result: dict[str, Any] | None = None
        with execute_tool_span(tracer, tool_name=call.name, step_idx=step_idx) as tool_span:
            try:
                result = self.registry.run(call.name, call.arguments)
                if isinstance(result, dict):
                    structured_result = result
                raw_dump = _dumps(result)
                observation, obs_truncated = truncate_observation(
                    self._overflow,
                    raw_dump,
                    step_idx,
                    token_limit=self.observation_token_limit,
                    char_cap=self.observation_char_limit,
                )
                is_error = False
            except SkillError as exc:
                observation = f"Error: {exc}"
                obs_truncated = False
                is_error = True
                _mark_span_error(tool_span, exc)
            except Exception as exc:
                observation = f"Error: {type(exc).__name__}: {exc}"
                obs_truncated = False
                is_error = True
                _mark_span_error(tool_span, exc)
        return AgentStep(
            # The assistant turn's text IS the ReAct thought (Yao et al., 2022); the caller passes
            # it in so the native path preserves the same reasoning trace as the text path.
            thought=thought,
            action=call.name,
            action_input=call.arguments,
            observation=observation,
            result=structured_result,
            is_error=is_error,
            observation_chars=len(observation),
            is_truncated=obs_truncated,
        )


def _assistant_tool_call_message(tool_calls: list[Any], text: str = "") -> dict[str, Any]:
    """Build the ``role=assistant`` message echoing the model's tool-call request, in the exact
    OpenAI shape the API requires before the matching ``role=tool`` result messages.

    The assistant ``content`` is echoed when the model returned text alongside its tool_calls: the
    OpenAI chat-completion contract makes ``content`` optional (nullable) — not forbidden — when
    ``tool_calls`` are present, and the function-calling guide's canonical pattern appends the full
    assistant message (content + tool_calls) back into history. ``text or None`` yields ``None`` for
    the empty-string default (byte-identical to the prior hardcoded ``None``) and the model's text
    when present.
    """
    return {
        "role": "assistant",
        "content": text or None,
        "tool_calls": [
            {
                "id": tc.call_id,
                "type": "function",
                "function": {"name": tc.name, "arguments": _dumps(tc.arguments)},
            }
            for tc in tool_calls
        ],
    }


def _mark_span_error(span: Any, exc: Exception) -> None:
    """Mark *span* ERROR and record *exc*; safe against the no-op span (OTEL disabled)."""
    try:
        from opentelemetry.trace import Status, StatusCode  # noqa: PLC0415

        span.set_status(Status(StatusCode.ERROR, str(exc)))
        span.record_exception(exc)
    except Exception:
        pass


_NATIVE_SYSTEM_PROMPT = """You are a world-consistency assistant for an open-world game project. \
Your job is to diagnose the project's content with the available tools and report what is wrong \
and the safe next step. You never write canon: any fix you find goes to a human review queue.

Use the provided tools (function-calling) to investigate. Call one or more tools per turn; the \
system runs each and returns its real result as a tool message. When you have enough to report, \
reply with a final text answer (no tool calls): a concise report for the human stating what is \
wrong and the safe next step. Never fabricate tool output."""


def parse_react_step(raw: str) -> ParsedStep:
    """Parse one model turn into a :class:`ParsedStep`.

    Robust to the usual model drift: a hallucinated ``Observation:`` (we cut it off — the system
    owns observations), prose/fences around the Action Input dict (handled by the shared
    ``extract_json_object``), and back-ticked or punctuated tool names.
    """
    # The model must not author observations; if it tried, keep only the text before the first one.
    text = raw.split("Observation:", 1)[0].strip()

    thought = _section_after(text, "Thought:", stop_markers=("Action:", "Final Answer:"))

    final = _section_after(text, "Final Answer:", stop_markers=())
    if final:
        return ParsedStep(thought=thought, final_answer=final)

    if "Action:" not in text:
        return ParsedStep(thought=thought)

    after_action = text.split("Action:", 1)[1]
    # The tool name is the first non-empty line after "Action:" (before any Action Input).
    name_region = after_action.split("Action Input:", 1)[0]
    action = _clean_action_name(name_region)
    if not action:
        return ParsedStep(thought=thought)

    action_input: dict[str, Any] = {}
    if "Action Input:" in after_action:
        input_region = after_action.split("Action Input:", 1)[1].strip()
        if input_region and input_region not in {"{}", "none", "None", "null"}:
            try:
                action_input = extract_json_object(input_region)
            except ValueError:
                action_input = {}
    return ParsedStep(thought=thought, action=action, action_input=action_input)


def _section_after(text: str, marker: str, *, stop_markers: tuple[str, ...]) -> str:
    if marker not in text:
        return ""
    region = text.split(marker, 1)[1]
    cut = len(region)
    for stop in stop_markers:
        idx = region.find(stop)
        if idx != -1:
            cut = min(cut, idx)
    return region[:cut].strip()


def _clean_action_name(region: str) -> str:
    for line in region.splitlines():
        candidate = line.strip().strip("`'\"* ").rstrip(".").strip()
        if candidate:
            return candidate
    return ""


def _trim_transcript(
    transcript: list[str],
    budget: int | None,
) -> tuple[list[str], int]:
    """Trim oldest turns until total tokens fit within budget (T4-C: real tiktoken counts).

    Returns (trimmed_transcript, n_omitted).
    When budget is None, returns (transcript, 0) unchanged.
    Never removes ALL turns: keeps at least the most recent one.

    This is the fallback hard-trim path used by _user_prompt() when context compression
    has already been applied but the view still exceeds the token budget (safety net).
    The primary compression path is compress_transcript() in context_compressor.py.
    """
    if budget is None or not transcript:
        return transcript, 0
    # Walk from newest to oldest, greedily keeping turns within budget using token counts
    kept: list[str] = []
    total_tokens = 0
    for turn in reversed(transcript):
        turn_tokens = count_tokens(turn)  # T4-C: true tiktoken count (not char÷4)
        if total_tokens + turn_tokens > budget and kept:
            # Adding this turn would exceed budget and we already have at least one — stop
            break
        kept.append(turn)
        total_tokens += turn_tokens
    kept.reverse()
    n_omitted = len(transcript) - len(kept)
    return kept, n_omitted


def _reflections_block(reflections: list[str]) -> str:
    """Render Reflexion episodic memory into a prompt block, shared by the text and native paths so
    both word it identically. Caller guards on non-empty ``reflections`` — this never renders an
    empty header."""
    return "Reflections from previous attempts:\n" + "\n".join(f"- {r}" for r in reflections)


def _user_prompt(
    goal: str,
    transcript: list[str],
    transcript_token_budget: int | None = None,  # T4-C: token budget (None = no limit)
    # Backward-compat alias — older call sites pass transcript_char_budget by keyword.
    # When only char_budget is supplied it is converted to a rough token budget (÷4).
    transcript_char_budget: int | None = None,
    *,
    # Reflexion episodic memory (Shinn et al., 2023). Rendered after the Goal header and before the
    # transcript. None/empty ⇒ the guard is skipped, so ``parts`` — and the returned string — is
    # byte-for-byte identical to the pre-Reflexion prompt.
    reflections: list[str] | None = None,
) -> str:
    # Resolve effective token budget: prefer token_budget, fall back to char_budget ÷4.
    effective_budget: int | None
    if transcript_token_budget is not None:
        effective_budget = transcript_token_budget
    elif transcript_char_budget is not None:
        effective_budget = max(1, transcript_char_budget // 4)
    else:
        effective_budget = None

    parts = [f"Goal: {goal}", ""]
    if reflections:
        parts.append(_reflections_block(reflections))
        parts.append("")
    if transcript:
        # Safety-net hard trim (compressor already ran; this catches edge cases)
        trimmed, n_omitted = _trim_transcript(transcript, effective_budget)
        if n_omitted:
            parts.append(f"[{n_omitted} earlier turns omitted]")  # marker before transcript
        parts.append("\n\n".join(trimmed))
        parts.append("")
        parts.append("Continue. Output your next Thought and Action, or a Final Answer.")
    else:
        parts.append("Begin. Output your first Thought and Action.")
    return "\n".join(parts)


def _compress_view(
    gateway: LLMGateway,
    transcript: list[str],
    token_budget: int | None,
    compression_threshold: float,
    keep_recent: int,
    cache: Any = None,
) -> tuple[list[str], Any]:
    """Apply read-time LLM compression to the transcript.  Returns (view, CompressionStats).

    This is the T4-A integration point: delegates to context_compressor.compress_transcript()
    when a token_budget is set, otherwise returns the transcript unchanged.
    The original transcript list is never modified (append-only guarantee preserved here).
    *cache* (a CompressionCache) memoises the compacted prefix across steps so the head is
    compressed incrementally rather than re-summarised from scratch each step.
    """
    from .context_compressor import CompressionStats, compress_transcript  # noqa: PLC0415

    if token_budget is None or not transcript:
        stats = CompressionStats()
        stats.pre_compress_tokens = sum(count_tokens(t) for t in transcript)
        stats.post_compress_tokens = stats.pre_compress_tokens
        return list(transcript), stats

    return compress_transcript(
        gateway=gateway,
        transcript=transcript,
        token_budget=token_budget,
        keep_recent=keep_recent,
        compression_threshold=compression_threshold,
        cache=cache,
    )


def _update_chat_span_from_telemetry(span: Any, gateway: LLMGateway) -> None:
    """Back-fill gen_ai.chat span from the most recent telemetry record.

    Sets ``gen_ai.request.model`` to the *real* model id the gateway resolved (e.g.
    "deepseek-v4-pro"), plus the actual input/output token counts. The model must come from
    telemetry — not the agent's task label — to satisfy the OTEL GenAI semantic convention,
    which defines ``gen_ai.request.model`` as the requested model identifier.

    Also sets ``gen_ai.response.model`` to the model the API *response body* reported (the model
    that actually answered) — but ONLY when the record carries a non-empty value. On a failover
    this is the secondary's id (≠ request.model = the primary). Offline fakes can't report a
    response model, so the attribute is simply left unset rather than back-filled with a guess.
    """
    records = gateway.telemetry.records
    if not records:
        return
    last = records[-1]
    try:
        if last.model:  # only overwrite the placeholder once we have a real id
            span.set_attribute("gen_ai.request.model", last.model)
        # Per OTEL GenAI conventions response.model is the model that answered. Set it only when
        # known (real provider); never echo the request-side model when the response is silent.
        if last.response_model:
            span.set_attribute("gen_ai.response.model", last.response_model)
        span.set_attribute("gen_ai.usage.input_tokens", last.input_tokens)
        span.set_attribute("gen_ai.usage.output_tokens", last.output_tokens)
    except Exception:
        pass  # span may be a no-op; never raise here


def _render_turn(
    thought: str, action: str | None, action_input: dict[str, Any], observation: str
) -> str:
    lines = [f"Thought: {thought}"]
    if action is not None:
        lines.append(f"Action: {action}")
        lines.append(f"Action Input: {_dumps(action_input)}")
    lines.append(f"Observation: {observation}")
    return "\n".join(lines)


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _render_native_transcript(messages: list[dict[str, Any]]) -> str:
    """Flatten the native tool-calling message history (system/user/assistant/tool roles) into the
    same kind of readable Thought/Action/Observation-ish text the "generate" budget-finish prompt
    expects — built via the plain ``gateway.complete()`` (system/user strings), never
    ``complete_with_tools``, so the finish call works with any provider regardless of native-tools
    support. Skips the leading system prompt (the finish call supplies its own); the goal is
    rendered by the caller, so this starts from the first user "Goal:" message.
    """
    lines: list[str] = []
    for message in messages[1:]:  # [0] is the native loop's own system prompt
        role = message.get("role", "")
        content = message.get("content") or ""
        tool_calls = message.get("tool_calls")
        if role == "assistant" and tool_calls:
            names = ", ".join(tc["function"]["name"] for tc in tool_calls)
            lines.append(f"[assistant] {content}\nAction(s) requested: {names}".strip())
        else:
            lines.append(f"[{role}] {content}")
    return "\n\n".join(lines)
