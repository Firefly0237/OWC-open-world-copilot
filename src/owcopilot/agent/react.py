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
from typing import Any

from pydantic import BaseModel, Field

from ..core.skills import SkillError, SkillRegistry
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

# Cap a single observation fed back into the prompt so a large tool result can't blow the context
# budget. ~1k tokens comfortably fits a typical tool result (an audit of a small/medium world);
# genuinely huge outputs are truncated with an explicit marker (the model sees it), never silently.
_OBSERVATION_CHAR_LIMIT = 4000

# Placeholder for gen_ai.request.model until the gateway resolves the real model behind the tier;
# back-filled from telemetry after each planning call (see _update_chat_span_from_telemetry).
_PENDING_MODEL = "(resolving)"


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
    # this — the ``observation`` string may be truncated by ``_OBSERVATION_CHAR_LIMIT`` and
    # cannot be relied on for parsing.
    result: dict[str, Any] | None = None
    is_error: bool = False
    # IN-6: real latency/cost from gateway CallRecords (not hardcoded; 0.0 only for mock tier)
    latency_ms: float = 0.0
    cost_usd: float = 0.0
    # IN-B4 T3: observation metadata after (possible) truncation
    observation_chars: int = 0   # len(observation) after truncation
    is_truncated: bool = False   # True iff observation was truncated by _truncate()


class AgentResult(BaseModel):
    goal: str
    final_answer: str
    # "finished": model emitted a Final Answer. "max_steps": ran out of step budget.
    stop_reason: str
    steps: list[AgentStep] = Field(default_factory=list)
    step_count: int = 0
    # Context compression accounting (T4-A)
    context_compressions: int = 0        # number of compression rounds triggered
    pre_compress_tokens: int = 0         # transcript tokens before first compression
    post_compress_tokens: int = 0        # transcript tokens after last compression


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
        observation_char_limit: int = _OBSERVATION_CHAR_LIMIT,
        allowed_skills: set[str] | None = None,        # IN-B2 T2: None = full manifest (compat)
        # T4-C: transcript_token_budget is the canonical param (tiktoken units).
        # transcript_char_budget is kept for backward compatibility: if only char_budget is
        # supplied it is converted to tokens via ÷4 with a deprecation note in docstring.
        transcript_token_budget: int | None = None,
        transcript_char_budget: int | None = 20_000,  # IN-B4 C1: None = no limit
        # T4-A: context compressor threshold (fraction of token_budget).
        compression_threshold: float = 0.70,
        compression_keep_recent: int = 3,
        agent_name: str = "world-consistency",        # T4-B: OTEL agent.name attribute
        # P2-a: opt into native OpenAI function-calling instead of the text ReAct format.
        # Default False ⇒ the text path runs exactly as before (zero behaviour change). Even when
        # True, the agent falls back to text if the routed provider does not support native tools
        # (provider_supports_tools probe), so an offline-fake run is never affected.
        use_native_tools: bool = False,
    ) -> None:
        if max_steps < 1:
            raise ValueError("max_steps must be >= 1")
        self.gateway = gateway
        self.registry = registry
        self.max_steps = max_steps
        self.task = task
        self.observation_char_limit = observation_char_limit
        self.allowed_skills = allowed_skills          # IN-B2 T2
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

    def run(self, goal: str) -> AgentResult:
        # P2-a: opt-in native function-calling loop, only when the provider supports it; otherwise
        # the canonical text ReAct loop below runs unchanged.
        if self._native_tools_available():
            return self._run_native_tools(goal)
        return self._run_text(goal)

    def _run_text(self, goal: str) -> AgentResult:
        # IN-B2 T2: use filtered manifest when allowed_skills is set
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
                        # T4-C: pass token-budget-compressed view instead of raw transcript
                        user=_user_prompt(goal, view, self.transcript_token_budget),
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
                        observation = _truncate(raw_dump, self.observation_char_limit)
                        obs_truncated = len(raw_dump) > self.observation_char_limit  # IN-B4 T3
                        is_error = False
                    except SkillError as exc:
                        observation = f"Error: {exc}"
                        obs_truncated = False  # IN-B4 T3: error string is not truncated
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
                        obs_truncated = False  # IN-B4 T3: error string is not truncated
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
                        observation_chars=len(observation),  # IN-B4 T3: len after truncation
                        is_truncated=obs_truncated,          # IN-B4 T3
                    )
                )
                transcript.append(
                    _render_turn(parsed.thought, parsed.action, parsed.action_input, observation)
                )

            root_span.set_attribute("agent.stop_reason", "max_steps")
            return AgentResult(
                goal=goal,
                final_answer=(
                    "Reached the step budget before finishing. "
                    "Latest findings are in the steps above; "
                    "re-run with a higher --max-steps to continue."
                ),
                stop_reason="max_steps",
                steps=steps,
                step_count=len(steps),
                context_compressions=total_compressions,
                pre_compress_tokens=first_pre_tokens,
                post_compress_tokens=last_post_tokens,
            )

    def _run_native_tools(self, goal: str) -> AgentResult:
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
                with gen_ai_chat_span(
                    tracer, model=_PENDING_MODEL, step_idx=step_idx
                ) as chat_span:
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
                # OpenAI contract so the following tool results correlate to these call ids).
                messages.append(_assistant_tool_call_message(resp.tool_calls))

                for call in resp.tool_calls:
                    snap_idx = len(self.gateway.telemetry.records)
                    step = self._execute_native_call(tracer, call, step_idx)
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

            root_span.set_attribute("agent.stop_reason", "max_steps")
            return AgentResult(
                goal=goal,
                final_answer=(
                    "Reached the step budget before finishing. "
                    "Latest findings are in the steps above; "
                    "re-run with a higher --max-steps to continue."
                ),
                stop_reason="max_steps",
                steps=steps,
                step_count=len(steps),
            )

    def _execute_native_call(self, tracer: Any, call: Any, step_idx: int) -> AgentStep:
        """Execute one native tool call via the registry and build its :class:`AgentStep`.

        Shares the registry dispatch, structured-result capture, observation truncation and
        execute_tool span with the text path — only the *source* of (name, args) differs (a parsed
        ``ToolCall`` instead of a text-parsed action). Returns the step with latency/cost left at
        0.0 for the caller to back-fill from telemetry.
        """
        structured_result: dict[str, Any] | None = None
        with execute_tool_span(tracer, tool_name=call.name, step_idx=step_idx) as tool_span:
            try:
                result = self.registry.run(call.name, call.arguments)
                if isinstance(result, dict):
                    structured_result = result
                raw_dump = _dumps(result)
                observation = _truncate(raw_dump, self.observation_char_limit)
                obs_truncated = len(raw_dump) > self.observation_char_limit
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
            thought="",  # native tool-calling carries no separate thought channel
            action=call.name,
            action_input=call.arguments,
            observation=observation,
            result=structured_result,
            is_error=is_error,
            observation_chars=len(observation),
            is_truncated=obs_truncated,
        )


def _assistant_tool_call_message(tool_calls: list[Any]) -> dict[str, Any]:
    """Build the ``role=assistant`` message echoing the model's tool-call request, in the exact
    OpenAI shape the API requires before the matching ``role=tool`` result messages."""
    return {
        "role": "assistant",
        "content": None,
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


def _user_prompt(
    goal: str,
    transcript: list[str],
    transcript_token_budget: int | None = None,  # T4-C: token budget (None = no limit)
    # Backward-compat alias — older call sites pass transcript_char_budget by keyword.
    # When only char_budget is supplied it is converted to a rough token budget (÷4).
    transcript_char_budget: int | None = None,
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


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"… [truncated {len(text) - limit} chars]"
