"""TelemetryCallbackHandler: per-call CallRecord accounting for the LangChain path (T2-S3).

Pins the field-by-field CallRecord mapping (usage/cached tokens/response_model/latency/task),
the task-label resolution (metadata ``owc_task`` > ``compact`` tag > default), the
``records_since`` snapshot semantics against callback-fed records, and sanitized failure rows.
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool

from owcopilot.llm.telemetry import CallRecord, TelemetryCollector
from owcopilot.llm.telemetry_callback import TelemetryCallbackHandler


class _BindableFake(GenericFakeChatModel):
    """Scripted fake accepting bind_tools (create_agent always binds)."""

    def bind_tools(self, tools, **kwargs):
        return self


class _RaisingFake(GenericFakeChatModel):
    """Chat model whose every call raises."""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise RuntimeError("provider down")


@tool
def lookup(query: str) -> str:
    """Look up a fact."""
    return f"fact:{query}"


def _usage_message(
    text: str = "", *, out_tok: int, tool_calls: list[dict[str, Any]] | None = None
) -> AIMessage:
    return AIMessage(
        content=text,
        tool_calls=tool_calls or [],
        usage_metadata={
            "input_tokens": 1000,
            "output_tokens": out_tok,
            "total_tokens": 1000 + out_tok,
            "input_token_details": {"cache_read": 400},
        },
        response_metadata={"model_name": "deepseek-v4-flash-0716"},
    )


def _scripted_agent_model() -> _BindableFake:
    return _BindableFake(
        messages=iter(
            [
                _usage_message(
                    out_tok=50,
                    tool_calls=[
                        {"name": "lookup", "args": {"query": "x"}, "id": "c0", "type": "tool_call"}
                    ],
                ),
                _usage_message("done", out_tok=7),
            ]
        )
    )


def _handler(collector: TelemetryCollector) -> TelemetryCallbackHandler:
    return TelemetryCallbackHandler(
        collector, tier="cheap", model="deepseek-v4-flash", transport_attempt_cap=3
    )


def test_agent_run_records_one_call_record_per_model_call_field_by_field() -> None:
    collector = TelemetryCollector()
    agent = create_agent(_scripted_agent_model(), [lookup])

    agent.invoke({"messages": [HumanMessage("q")]}, config={"callbacks": [_handler(collector)]})

    assert len(collector.records) == 2  # one per model call: planning turn + final turn
    first = collector.records[0]
    assert first.task == "agent_react"  # the default task label
    assert first.tier == "cheap"
    assert first.model == "deepseek-v4-flash"
    assert first.input_tokens == 1000
    assert first.output_tokens == 50
    assert first.cached_input_tokens == 400  # provider prefix-cache hit subset
    assert first.cache_hit is False
    assert first.response_model == "deepseek-v4-flash-0716"
    assert first.latency_ms > 0.0
    assert first.gateway_invocations == 1
    assert first.transport_attempt_cap == 3
    assert collector.records[1].output_tokens == 7
    # The cost model is untouched: CallRecord.cost_usd prices these tokens as before.
    assert first.cost_usd > 0.0


def test_records_since_snapshot_isolates_the_run() -> None:
    collector = TelemetryCollector()
    collector.record(CallRecord(task="other", tier="cheap", input_tokens=1, output_tokens=1))
    agent = create_agent(_scripted_agent_model(), [lookup])

    snap = len(collector.records)
    agent.invoke({"messages": [HumanMessage("q")]}, config={"callbacks": [_handler(collector)]})

    since = collector.records_since(snap)
    assert len(since) == 2
    assert all(r.task == "agent_react" for r in since)
    assert len(collector.records) == 3  # the pre-existing record is untouched


def test_compact_tag_labels_the_task() -> None:
    collector = TelemetryCollector()
    chat = _BindableFake(messages=iter([_usage_message("summary", out_tok=3)]))

    chat.invoke(
        [HumanMessage("summarise")],
        config={"callbacks": [_handler(collector)], "tags": ["compact"]},
    )

    (record,) = collector.records
    assert record.task == "compact"


def test_owc_task_metadata_takes_precedence_over_tags() -> None:
    collector = TelemetryCollector()
    chat = _BindableFake(messages=iter([_usage_message("x", out_tok=3)]))

    chat.invoke(
        [HumanMessage("q")],
        config={
            "callbacks": [_handler(collector)],
            "tags": ["compact"],
            "metadata": {"owc_task": "distill"},
        },
    )

    (record,) = collector.records
    assert record.task == "distill"


def test_failed_call_records_only_sanitized_category_and_attempt_counters() -> None:
    collector = TelemetryCollector()
    chat = _RaisingFake(messages=iter([]))

    with pytest.raises(RuntimeError, match="provider down"):
        chat.invoke([HumanMessage("q")], config={"callbacks": [_handler(collector)]})

    (record,) = collector.records
    assert record.success is False
    assert record.error_category == "provider_error"
    assert record.gateway_invocations == 1
    assert record.transport_attempt_cap == 3
    assert record.input_tokens == record.output_tokens == 0
    assert "provider down" not in repr(record)


def test_usage_free_fake_degrades_to_zero_tokens() -> None:
    collector = TelemetryCollector()
    chat = _BindableFake(messages=iter([AIMessage(content="plain")]))

    chat.invoke([HumanMessage("q")], config={"callbacks": [_handler(collector)]})

    (record,) = collector.records
    assert (record.input_tokens, record.output_tokens, record.cached_input_tokens) == (0, 0, 0)
    assert record.response_model == ""
