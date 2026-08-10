# LangChain callbacks → 自研 CallRecord 遥测桥:每次 chat model 调用落一条记录。
# 关键:成本模型/归账语义不变(telemetry.py PRICES + records_since 快照仍是唯一口径);
#   task 标签从 invoke config 的 metadata/tags 取(压缩调用 tags=["compact"]),默认 agent_react;
#   失败调用也记脱敏类别/时延/调用上限，不存异常原文、提示词或 headers。
"""LangChain callback handler feeding the self-built per-call telemetry.

The LangGraph agent loop no longer routes model calls through ``LLMGateway.complete``, so the
gateway's per-call ``CallRecord`` accounting moves to the standard callback surface: attach one
:class:`TelemetryCallbackHandler` via ``agent.invoke(input, config={"callbacks": [handler]})``
and every chat-model call in the run — planning calls, and middleware-initiated calls such as
context compaction (which inherit the environment callbacks through the config contextvar) —
appends one :class:`~owcopilot.llm.telemetry.CallRecord` to the same
:class:`~owcopilot.llm.telemetry.TelemetryCollector` the rest of the stack reads.

Field mapping (kept in lockstep with the gateway's records):

* ``task`` — ``metadata["owc_task"]`` when the call's config carries it, else the ``"compact"``
  tag (the context-compaction convention), else the handler's ``default_task``.
* ``tier`` / ``model`` — fixed at construction (the host knows which tier/model id it wired).
* ``input_tokens`` / ``output_tokens`` / ``cached_input_tokens`` — from the response message's
  standard ``usage_metadata`` (``input_token_details.cache_read`` is the provider prefix-cache
  hit count), the same source :class:`~owcopilot.llm.chat_model.ChatModelProvider` uses.
* ``response_model`` — ``response_metadata.model_name`` (the model that actually answered).
* ``latency_ms`` — wall clock between this call's start/end callbacks (paired by ``run_id``).
* ``cache_hit`` — always ``False``: agent-path calls never pass the client cache (the gateway
  deliberately does not cache tool-loop calls either).

A failed call (``on_llm_error``) records a zero-token row with only a stable error category and
attempt ceiling. Exception text, request bodies and headers are deliberately never copied.
"""

from __future__ import annotations

import threading
import time
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import LLMResult

from .chat_model import _response_model_of, _usage_of
from .gateway import _classify_provider_error
from .telemetry import CallRecord, TelemetryCollector


class TelemetryCallbackHandler(BaseCallbackHandler):
    """One :class:`CallRecord` per chat-model call, appended to *collector*."""

    def __init__(
        self,
        collector: TelemetryCollector,
        *,
        tier: str,
        model: str,
        default_task: str = "agent_react",
        transport_attempt_cap: int = 1,
    ) -> None:
        self.collector = collector
        self.tier = tier
        self.model = model
        self.default_task = default_task
        self.transport_attempt_cap = max(1, transport_attempt_cap)
        # run_id -> (perf-counter start, resolved task label). Locked: middleware-initiated calls
        # and (later) parallel agents may overlap on one handler instance.
        self._inflight: dict[UUID, tuple[float, str]] = {}
        self._lock = threading.Lock()

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        task = str((metadata or {}).get("owc_task") or "")
        if not task and tags and "compact" in tags:
            task = "compact"
        with self._lock:
            self._inflight[run_id] = (time.perf_counter(), task or self.default_task)

    def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> Any:
        with self._lock:
            started = self._inflight.pop(run_id, None)
        if started is None:  # unpaired end (defensive): still record the call, latency unknown
            latency_ms, task = 0.0, self.default_task
        else:
            latency_ms = (time.perf_counter() - started[0]) * 1000
            task = started[1]
        message = self._message_of(response)
        if message is None:
            in_tok = out_tok = cached = 0
            response_model = ""
        else:
            in_tok, out_tok, cached = _usage_of(message)
            response_model = _response_model_of(message)
        self.collector.record(
            CallRecord(
                task=task,
                tier=self.tier,
                input_tokens=in_tok,
                output_tokens=out_tok,
                cached_input_tokens=cached,
                cache_hit=False,
                latency_ms=latency_ms,
                model=self.model,
                response_model=response_model,
                gateway_invocations=1,
                transport_attempt_cap=self.transport_attempt_cap,
            )
        )

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> Any:
        with self._lock:
            started = self._inflight.pop(run_id, None)
        if started is None:
            latency_ms, task = 0.0, self.default_task
        else:
            latency_ms = (time.perf_counter() - started[0]) * 1000
            task = started[1]
        category = (
            _classify_provider_error(error) if isinstance(error, Exception) else "provider_error"
        )
        self.collector.record(
            CallRecord(
                task=task,
                tier=self.tier,
                input_tokens=0,
                output_tokens=0,
                latency_ms=latency_ms,
                model=self.model,
                gateway_invocations=1,
                transport_attempt_cap=self.transport_attempt_cap,
                success=False,
                error_category=category,
            )
        )

    @staticmethod
    def _message_of(response: LLMResult) -> AIMessage | None:
        """The response's AIMessage, or None for a degenerate result (no usage to read then)."""
        if not response.generations or not response.generations[0]:
            return None
        message = getattr(response.generations[0][0], "message", None)
        return message if isinstance(message, AIMessage) else None
