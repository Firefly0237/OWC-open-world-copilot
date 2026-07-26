# 自研 agent middleware 族(T2-S4):LangGraph create_agent 宿主上的四件自研资产。
# ObservationOverflow=观测截断+错误恢复(统一信封:失败经 skills/errors 归类为 error_code,
#   与成功同构、模型可按码自恢复)+串行锁+execute_tool span+步耗时归因;
# ContextCompression=读时压缩(视图投影,state 永不改,压缩批=完整 AIMessage+ToolMessages 轮);
# MemoryInjection=Reflexion 反思注入 system 后缀(无反思时零 override,byte-identical);
# BudgetFinish=预算耗尽收尾(force/generate 双形态)+ owc_stop_reason 三态输出;
# TelemetrySpan=gen_ai.chat span 复刻(usage 从应答直接回填)。
"""Self-built agent middleware for the LangGraph ``create_agent`` host.

These carry the four defensible self-built assets of the ReAct loop onto the standard host:

* :class:`ObservationOverflowMiddleware` — ``wrap_tool_call``: recoverable observation
  truncation (marker + run-scoped overflow store, the ``read_observation`` restore path),
  error-to-observation recovery as a **unified response envelope** (the default ToolNode re-raises
  tool exceptions — the "errors become observations" contract must be self-built; the exception is
  classified into a machine-readable ``error_code`` via :mod:`owcopilot.skills.errors` so the
  failure exit is isomorphic to success and the system-prompt recovery layer can act on the code),
  a run-level lock that keeps one turn's
  parallel tool calls serial (behaviour parity; skill concurrency is unaudited), the
  ``execute_tool`` OTEL span, and per-step latency/cost attribution via the telemetry
  ``records_since`` snapshot (written to ``ToolMessage.response_metadata``).
* :class:`ContextCompressionMiddleware` — ``wrap_model_call``: *read-time* transcript
  compression. The compressed view exists only for the model call
  (``request.override(messages=view)``); graph state is never rewritten (the opposite of the
  stock ``SummarizationMiddleware``, which removes state messages — that destructiveness is
  exactly why this stays self-built). The compression batch unit is a complete round
  (one AIMessage plus its ToolMessages) so an assistant/tool pair is never split — an orphan
  ToolMessage would 400 on the OpenAI protocol — and the summary marker enters the view as a
  HumanMessage, never a fabricated assistant/tool pair.
* :class:`MemoryInjectionMiddleware` — ``wrap_model_call``: Reflexion episodic memory as a
  system-message suffix. ``None``/empty reflections mean **no override at all** — the request
  reaching the model is byte-identical to a run without this middleware.
* :class:`BudgetFinishMiddleware` — ``after_agent``: resolves the run's stop reason into the
  non-private ``owc_stop_reason`` state field (``finished`` / ``max_steps`` /
  ``max_steps_generate``) and, in ``generate`` mode, spends ONE extra chat call (direct
  ``chat_model.invoke``, off the model node — uncounted by ``ModelCallLimitMiddleware``) to
  produce an honest final answer from the transcript; a failed finish call falls back to the
  ``force`` stop with a WARNING, never crashes.
* :class:`TelemetrySpanMiddleware` — ``wrap_model_call``: the ``gen_ai.chat`` span per model
  call, with usage/response-model back-filled straight from the returned ``AIMessage`` (no more
  read-the-last-telemetry-record indirection).

Composition notes (pinned by tests): middleware *wrap* hooks compose with the FIRST list entry
as the outermost layer; ``after_agent`` hooks run in REVERSE declaration order; a
``ModelCallLimitMiddleware(exit_behavior="end")`` jump still runs the ``after_agent`` chain, so
:class:`BudgetFinishMiddleware` sees the exhausted run.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Annotated, Any, Literal, NotRequired

from langchain.agents.middleware import (
    AgentMiddleware,
    AgentState,
    ModelRequest,
    ModelResponse,
    ToolCallRequest,
)
from langchain.agents.middleware.types import PrivateStateAttr
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.channels.untracked_value import UntrackedValue
from langgraph.runtime import Runtime
from langgraph.types import Command

from ..config import CONTEXT_COMPRESSION_KEEP_RECENT, CONTEXT_COMPRESSION_THRESHOLD
from ..llm.chat_model import _response_model_of, _usage_of
from ..llm.otel_bridge import execute_tool_span, gen_ai_chat_span, get_tracer_or_noop
from ..llm.telemetry import TelemetryCollector
from ..skills import SkillError
from ..skills.errors import ErrorCode, classify_error, error_envelope
from ..skills.langchain_tools import dumps_observation
from ..skills.observation_overflow import truncate_observation
from .context_compressor import CompressionCache, _is_checkpoint, compress_transcript

logger = logging.getLogger(__name__)

# The step-budget-exhausted message for `early_stopping="force"` and the honest fallback wording
# when the "generate" finish call itself fails. Verbatim from the pre-migration loop (the result
# assembler substitutes it for the limiter's artificial message, byte-for-byte).
_MAX_STEPS_MESSAGE = (
    "Reached the step budget before finishing. "
    "Latest findings are in the steps above; "
    "re-run with a higher --max-steps to continue."
)

# System prompt for the ONE extra "generate" finish call (LangChain AgentExecutor's
# early_stopping_method="generate"): explicitly forbids another tool call and asks the model to
# answer from the transcript alone, or admit it cannot and summarize what it has. Kept generic (no
# per-goal specifics) so it never overfits one call site. Verbatim from the pre-migration loop.
_GENERATE_FINISH_SYSTEM = (
    "You are a world-consistency assistant for an open-world game project. The tool-call step "
    "budget for this investigation is exhausted — you may NOT call another tool and no Action will "
    "be executed even if you write one. Based ONLY on the goal and the transcript of Thoughts/"
    "Actions/Observations below, give your Final Answer now: what you found, and the safe next "
    "step. If the transcript does not have enough to fully answer, say so explicitly and summarize "
    "what you do know. Reply in the form:\n\nThought: <your reasoning>\nFinal Answer: <your answer>"
)

#: The stock limiter's artificial exhaustion message prefix (langchain's
#: ``_build_limit_exceeded_message``). Its presence as the last assistant message is the honest
#: discriminator between "budget exhausted" and "finished exactly at the budget" — the run-level
#: call count alone cannot tell those apart.
_LIMIT_MESSAGE_PREFIX = "Model call limits exceeded"


def _mark_span_error(span: Any, exc: BaseException) -> None:
    """Mark *span* ERROR and record *exc*; safe against the no-op span (OTEL disabled)."""
    try:
        from opentelemetry.trace import Status, StatusCode  # noqa: PLC0415

        span.set_status(Status(StatusCode.ERROR, str(exc)))
        span.record_exception(exc)
    except Exception:
        pass


# --------------------------------------------------------------------------- message rendering

_ROLE_OF = {"human": "user", "ai": "assistant"}


def _render_message(message: AnyMessage) -> str:
    """One message as readable transcript text (compaction input / finish-call transcript)."""
    role = _ROLE_OF.get(message.type, message.type)
    text = str(message.text or "")
    if isinstance(message, AIMessage) and message.tool_calls:
        names = ", ".join(str(tc.get("name", "")) for tc in message.tool_calls)
        return f"[{role}] {text}\nAction(s) requested: {names}".strip()
    return f"[{role}] {text}"


def _render_round(round_messages: list[AnyMessage]) -> str:
    """A complete round (AIMessage + its ToolMessages) as one transcript turn string."""
    return "\n".join(_render_message(m) for m in round_messages)


def _split_rounds(messages: list[AnyMessage]) -> tuple[list[AnyMessage], list[list[AnyMessage]]]:
    """Split the (system-less) message list into the leading block and complete rounds.

    The leading block (everything before the first AIMessage — the goal message) is always kept
    verbatim. Each round starts at an AIMessage and carries every following non-AI message (its
    ToolMessages), so a round is exactly the protocol unit that must never be split.
    """
    first_ai = next((i for i, m in enumerate(messages) if isinstance(m, AIMessage)), None)
    if first_ai is None:
        return list(messages), []
    rounds: list[list[AnyMessage]] = []
    for message in messages[first_ai:]:
        if isinstance(message, AIMessage):
            rounds.append([message])
        else:
            rounds[-1].append(message)
    return list(messages[:first_ai]), rounds


# --------------------------------------------------------------------------- observation overflow


class ObservationOverflowMiddleware(AgentMiddleware):
    """Tool-call wrapper: serial execution, error recovery, recoverable truncation, span, cost.

    Run-scoped by construction: the host builds ONE instance per run and hands it the run's
    overflow store (the same dict it binds for the ``read_observation`` skill), so obs-ids and
    step indices never leak across runs.
    """

    def __init__(
        self,
        *,
        overflow: dict[str, str],
        token_limit: int,
        char_cap: int,
        collector: TelemetryCollector | None = None,
        tracer: Any | None = None,
    ) -> None:
        super().__init__()
        self.overflow = overflow
        self.token_limit = token_limit
        self.char_cap = char_cap
        self.collector = collector
        self._tracer = tracer
        self._lock = threading.Lock()
        self._step_idx = 0

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        tracer = self._tracer if self._tracer is not None else get_tracer_or_noop()
        tool_name = str(request.tool_call.get("name") or "")
        # DELIBERATELY SERIAL (C-6) — not an accidental bottleneck. LangGraph fans a turn's parallel
        # tool_calls out to worker threads; this run-level lock forces them back into single-file
        # execution because the body mutates shared, non-thread-safe run state that would race:
        #   1. ``self._step_idx`` — a plain read-then-increment (below), so parallel calls would
        #      hand out duplicate/rotated step indices.
        #   2. the telemetry snapshot window — ``snap = len(collector.records)`` here paired with
        #      ``records_since(snap)`` after the handler attributes THIS call's CallRecords to THIS
        #      step. ``TelemetryCollector.records`` is a plain list appended to by LLM skills; two
        #      handlers interleaving would corrupt the append and overlap the windows, mis-costing.
        #   3. the overflow store — ``truncate_observation`` keys tails by step_idx + len(store),
        #      then writes the shared ``self.overflow`` dict; parallel writes would collide ids.
        # On top of that, each skill handler opens its own ProjectContext and skill concurrency is
        # unaudited (shared parse/embed caches). Relaxing this would require making all three pieces
        # of run state race-free AND auditing every handler for thread safety — until then serial is
        # the correct, safe behaviour (and matches the pre-migration sequential loop byte-for-byte).
        with self._lock:
            step_idx = self._step_idx
            self._step_idx += 1
            snap = len(self.collector.records) if self.collector is not None else 0
            with execute_tool_span(tracer, tool_name=tool_name, step_idx=step_idx) as span:
                try:
                    result = handler(request)
                except SkillError as exc:
                    _mark_span_error(span, exc)
                    result = self._error_message(request, exc, f"Error: {exc}")
                except Exception as exc:  # a tool blew up; report as observation, don't crash
                    _mark_span_error(span, exc)
                    result = self._error_message(
                        request, exc, f"Error: {type(exc).__name__}: {exc}"
                    )
                else:
                    if isinstance(result, ToolMessage) and result.status == "error":
                        _mark_span_error(span, RuntimeError(str(result.content)))
            if isinstance(result, ToolMessage):
                self._post_process(result, step_idx)
                if self.collector is not None:
                    records = self.collector.records_since(snap)
                    result.response_metadata["owc_latency_ms"] = sum(r.latency_ms for r in records)
                    result.response_metadata["owc_cost_usd"] = sum(r.cost_usd for r in records)
            return result

    @staticmethod
    def _error_message(request: ToolCallRequest, exc: BaseException, message: str) -> ToolMessage:
        """The recoverable error observation as the unified skill envelope (E-1).

        The content is the canonical envelope ``{"status": "error", "error_code": <code>,
        "message": ...}`` (serialized with the SAME serializer as a success observation), so the
        model receives a machine-readable code it can recover from per the system prompt's recovery
        layer — not free text. ``message`` keeps the pre-migration ``Error: ...`` wording verbatim
        (it is the compaction checkpoint marker; see
        :func:`~owcopilot.skills.errors.error_envelope`) and ``owc_error_code`` is stamped for
        off-band consumers (console error rows, telemetry)."""
        code = classify_error(exc)
        tool_message = ToolMessage(
            content=dumps_observation(error_envelope(code, message)),
            tool_call_id=str(request.tool_call.get("id") or ""),
            name=str(request.tool_call.get("name") or "") or None,
            status="error",
        )
        tool_message.response_metadata["owc_error_code"] = code.value
        return tool_message

    def _post_process(self, message: ToolMessage, step_idx: int) -> None:
        """Truncate an oversized observation (recoverably) and stamp the observation metadata."""
        truncated = False
        if message.status != "error" and isinstance(message.content, str):
            observation, truncated = truncate_observation(
                self.overflow,
                message.content,
                step_idx,
                token_limit=self.token_limit,
                char_cap=self.char_cap,
            )
            if truncated:
                message.content = observation
        length = len(message.content) if isinstance(message.content, str) else 0
        message.response_metadata["owc_observation_chars"] = length
        message.response_metadata["owc_is_truncated"] = truncated
        # Uniform envelope status on every skill exit (E-1): "ok" on success, "error" on failure.
        # An error ToolMessage produced WITHOUT going through _error_message (a tool that returned
        # status="error" itself, not via a raised exception) still gets a fallback code so no
        # error exit is ever code-less.
        is_error = message.status == "error"
        message.response_metadata["owc_status"] = "error" if is_error else "ok"
        if is_error:
            message.response_metadata.setdefault("owc_error_code", ErrorCode.INTERNAL.value)


# --------------------------------------------------------------------------- context compression


class ContextCompressionMiddleware(AgentMiddleware):
    """Read-time transcript compression: the model sees a compacted view; state is untouched.

    Round-based: the token gate and the compaction batch operate on complete rounds (AIMessage +
    its ToolMessages), never splitting a pair. Compression runs through
    :func:`~owcopilot.agent.context_compressor.compress_transcript` (same trigger gate,
    checkpoint protection, incremental cache and marker format as the pre-migration path) with
    the injected chat model as the compaction caller — that call is tagged ``compact`` so the
    telemetry callback attributes it separately from planning calls.

    Run-scoped: one instance per run (the compression cache and the cumulative stats —
    ``context_compressions`` / ``pre_compress_tokens`` / ``post_compress_tokens``, read by the
    result assembler — are per-run state).
    """

    def __init__(
        self,
        *,
        chat_model: BaseChatModel,
        token_budget: int | None,
        compression_threshold: float = CONTEXT_COMPRESSION_THRESHOLD,
        keep_recent: int = CONTEXT_COMPRESSION_KEEP_RECENT,
        tracer: Any | None = None,
        model: str = "",
    ) -> None:
        super().__init__()
        self.chat_model = chat_model
        self.token_budget = token_budget
        self.compression_threshold = compression_threshold
        self.keep_recent = keep_recent
        # Run tracer + model id: the compaction call gets its OWN gen_ai.chat span (a child of the
        # planning span) so its latency is measured apart from the planning call it runs inside.
        self._tracer = tracer
        self._model = model
        self._cache = CompressionCache()
        self.context_compressions = 0
        self.pre_compress_tokens = 0
        self.post_compress_tokens = 0

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any] | AIMessage:
        if self.token_budget is None:
            return handler(request)
        leading, rounds = _split_rounds(list(request.messages))
        if not rounds:
            return handler(request)
        rendered = [_render_round(r) for r in rounds]
        _view_strs, stats = compress_transcript(
            None,
            rendered,
            self.token_budget,
            keep_recent=self.keep_recent,
            compression_threshold=self.compression_threshold,
            cache=self._cache,
            chat_model=self.chat_model,
            tracer=self._tracer,
            model=self._model,
        )
        if not stats.triggered:
            # Under threshold (or graceful fallback on a failed compact call): untouched request.
            return handler(request)
        if self.context_compressions == 0:
            self.pre_compress_tokens = stats.pre_compress_tokens
        self.post_compress_tokens = stats.post_compress_tokens
        self.context_compressions += stats.context_compressions
        # Rebuild the message-level view mirroring compress_transcript's split: protected recent
        # tail verbatim, checkpoint rounds verbatim ahead of the marker, compactable head replaced
        # by the summary marker as a HumanMessage (never a fabricated assistant/tool pair).
        keep = max(1, min(self.keep_recent, len(rounds)))
        head, tail = rounds[: len(rounds) - keep], rounds[len(rounds) - keep :]
        view: list[AnyMessage] = list(leading)
        for round_messages, text in zip(head, rendered[: len(head)], strict=True):
            if _is_checkpoint(text):
                view.extend(round_messages)
        view.append(HumanMessage(content=stats.summary_marker))
        for round_messages in tail:
            view.extend(round_messages)
        return handler(request.override(messages=view))


# --------------------------------------------------------------------------- memory injection


def _reflections_block(reflections: list[str]) -> str:
    """Render Reflexion episodic memory into a prompt block (wording carried over verbatim).

    Caller guards on non-empty ``reflections`` — this never renders an empty header.
    """
    return "Reflections from previous attempts:\n" + "\n".join(f"- {r}" for r in reflections)


class MemoryInjectionMiddleware(AgentMiddleware):
    """Reflexion memory as a system-message suffix; absent reflections leave the request alone.

    ``None``/``[]`` short-circuits to ``handler(request)`` with NO override, so the request the
    model receives is byte-identical to a run without reflections (pinned by test).
    """

    def __init__(self, reflections: list[str] | None = None) -> None:
        super().__init__()
        self.reflections = reflections

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any] | AIMessage:
        if not self.reflections:
            return handler(request)
        block = _reflections_block(self.reflections)
        base = request.system_message
        content = block if base is None else f"{base.text}\n\n{block}"
        return handler(request.override(system_message=SystemMessage(content=content)))


# --------------------------------------------------------------------------- budget finish


class BudgetFinishState(AgentState[Any]):
    """Agent state extended with the run's stop reason (non-private: visible in invoke output)."""

    owc_stop_reason: NotRequired[str]
    # Mirror of ModelCallLimitMiddleware's run counter declaration — annotation-identical so the
    # two schemas merge onto one channel. Declared so this middleware's hooks are guaranteed to
    # receive the key; read-only here (only the limiter increments it).
    run_model_call_count: NotRequired[Annotated[int, UntrackedValue, PrivateStateAttr]]


class BudgetFinishMiddleware(AgentMiddleware[BudgetFinishState, Any, Any]):
    """Resolves the stop reason after the run — and, opt-in, generates an honest budget finish.

    Pairs with ``ModelCallLimitMiddleware(run_limit=..., exit_behavior="end")``: exhaustion is
    detected from the run-level call count PLUS the limiter's artificial exhaustion message being
    the last assistant message (the count alone cannot distinguish "exhausted" from "finished on
    exactly the last allowed call"). The ``generate`` finish call goes straight to the chat model
    — off the model node — so it is neither counted nor blocked by the limiter.
    """

    state_schema = BudgetFinishState

    def __init__(
        self,
        *,
        chat_model: BaseChatModel,
        run_limit: int,
        early_stopping: Literal["force", "generate"] = "force",
        goal: str = "",
    ) -> None:
        super().__init__()
        if early_stopping not in ("force", "generate"):
            raise ValueError(
                f"early_stopping must be 'force' or 'generate', got {early_stopping!r}"
            )
        self.chat_model = chat_model
        self.run_limit = run_limit
        self.early_stopping = early_stopping
        self.goal = goal

    def after_agent(self, state: BudgetFinishState, runtime: Runtime[Any]) -> dict[str, Any] | None:
        if not self._budget_exhausted(state):
            return {"owc_stop_reason": "finished"}
        if self.early_stopping != "generate":
            return {"owc_stop_reason": "max_steps"}
        transcript = "\n\n".join(_render_message(m) for m in state["messages"])
        user = (
            f"Goal: {self.goal}\n\n{transcript}\n\n"
            "The step budget above is exhausted. Do not propose another action."
        )
        try:
            reply = self.chat_model.invoke(
                [
                    SystemMessage(content=_GENERATE_FINISH_SYSTEM),
                    HumanMessage(content=user),
                ]
            )
        except Exception:
            logger.warning(
                "BudgetFinishMiddleware: early_stopping='generate' finish call failed; "
                "falling back to the 'force' stop.",
                exc_info=True,
            )
            return {"owc_stop_reason": "max_steps"}
        final_answer = _final_answer_of(str(reply.text)) or _MAX_STEPS_MESSAGE
        return {
            "owc_stop_reason": "max_steps_generate",
            "messages": [AIMessage(content=final_answer)],
        }

    def _budget_exhausted(self, state: BudgetFinishState) -> bool:
        if int(state.get("run_model_call_count", 0)) < self.run_limit:
            return False
        messages = state.get("messages") or []
        last_ai = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
        return last_ai is not None and str(last_ai.text or "").startswith(_LIMIT_MESSAGE_PREFIX)


def _final_answer_of(raw: str) -> str:
    """The finish reply's answer: the text after its ``Final Answer:`` marker (the form the
    finish system prompt requests), else the whole reply — same tolerance as the old loop."""
    if "Final Answer:" in raw:
        return raw.split("Final Answer:", 1)[1].strip()
    return raw.strip()


# --------------------------------------------------------------------------- gen_ai.chat span


class TelemetrySpanMiddleware(AgentMiddleware):
    """The ``gen_ai.chat`` OTEL span per model call (one per planning call, step-indexed).

    Usage tokens and the response model are back-filled straight from the AIMessage the handler
    returns — replacing the old read-the-last-telemetry-record indirection. Run-scoped (the step
    index counts this run's model calls).
    """

    def __init__(self, *, model: str = "", tracer: Any | None = None) -> None:
        super().__init__()
        self.model = model
        self._tracer = tracer
        self._step_idx = 0

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any] | AIMessage:
        tracer = self._tracer if self._tracer is not None else get_tracer_or_noop()
        step_idx = self._step_idx
        self._step_idx += 1
        with gen_ai_chat_span(tracer, model=self.model or "(unknown)", step_idx=step_idx) as span:
            try:
                response = handler(request)
            except Exception as exc:
                _mark_span_error(span, exc)
                raise
            message = next((m for m in response.result if isinstance(m, AIMessage)), None)
            if message is not None:
                # Safe on the no-op span too (its set_attribute is a no-op, never raises).
                in_tok, out_tok, _cached = _usage_of(message)
                span.set_attribute("gen_ai.usage.input_tokens", in_tok)
                span.set_attribute("gen_ai.usage.output_tokens", out_tok)
                response_model = _response_model_of(message)
                if response_model:
                    span.set_attribute("gen_ai.response.model", response_model)
            return response
