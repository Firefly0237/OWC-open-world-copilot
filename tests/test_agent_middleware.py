"""Self-built agent middleware on the create_agent host (T2-S4).

Per-middleware behaviour pins (error recovery, recoverable truncation, serial tool execution,
read-time compression that never touches state, byte-identical memory injection, the
force/generate budget finish and the ``owc_stop_reason`` tri-state) plus the composition-order
contracts: wrap hooks compose first-in-list-outermost, ``after_agent`` runs in reverse
declaration order, and a ``ModelCallLimitMiddleware`` 'end' jump still reaches
``BudgetFinishMiddleware``.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, ModelCallLimitMiddleware
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import Field

from owcopilot.agent.middleware import (
    BudgetFinishMiddleware,
    ContextCompressionMiddleware,
    MemoryInjectionMiddleware,
    ObservationOverflowMiddleware,
    TelemetrySpanMiddleware,
)
from owcopilot.fakes import scripted_chat_model
from owcopilot.llm.otel_bridge import build_test_tracer
from owcopilot.llm.telemetry import CallRecord, TelemetryCollector
from owcopilot.llm.telemetry_callback import TelemetryCallbackHandler
from owcopilot.skills import CostTier, SideEffect, Skill, SkillParameter, SkillRegistry
from owcopilot.skills.langchain_tools import as_structured_tools, dumps_observation
from owcopilot.skills.observation_overflow import bind_overflow, read_observation

# --------------------------------------------------------------------------- fixtures


def _skill(name: str, handler, parameters=()) -> Skill:
    return Skill(
        name=name,
        description=f"{name} skill.",
        cost_tier=CostTier.DETERMINISTIC,
        side_effect=SideEffect.READ_ONLY,
        handler=handler,
        parameters=parameters,
    )


def _boom() -> dict[str, Any]:
    raise RuntimeError("boom")


def _registry(extra: list[Skill] | None = None) -> SkillRegistry:
    registry = SkillRegistry()
    registry.register(
        _skill(
            "echo",
            lambda v: {"value": v},
            (SkillParameter("v", "string", "value", required=True),),
        )
    )
    registry.register(_skill("big", lambda: {"blob": "x" * 2000}))
    registry.register(_skill("boom", _boom))
    for skill in extra or []:
        registry.register(skill)
    return registry


class _RecordingModelMiddleware(AgentMiddleware):
    """Innermost probe capturing exactly what each model call was handed."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[tuple[Any, list[Any]]] = []

    def wrap_model_call(self, request, handler):
        self.requests.append((request.system_message, list(request.messages)))
        return handler(request)


class _RecordingFinishFake(GenericFakeChatModel):
    """Finish-call fake recording every message list it was invoked with."""

    seen: list[Any] = Field(default_factory=list)

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(list(messages))
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


class _RaisingFake(GenericFakeChatModel):
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise RuntimeError("finish model down")


def _oo(**overrides: Any) -> ObservationOverflowMiddleware:
    kwargs: dict[str, Any] = {"overflow": {}, "token_limit": 2000, "char_cap": 8000}
    kwargs.update(overrides)
    return ObservationOverflowMiddleware(**kwargs)


def _run(script, registry, middleware, config=None):
    agent = create_agent(
        scripted_chat_model(script), as_structured_tools(registry), middleware=middleware
    )
    return agent.invoke({"messages": [HumanMessage("Goal: test goal")]}, config=config or {})


def _tool_messages(messages) -> list[ToolMessage]:
    return [m for m in messages if isinstance(m, ToolMessage)]


# --------------------------------------------------------------------------- observation overflow


def test_tool_exception_becomes_error_observation_and_loop_recovers() -> None:
    result = _run([("boom", {}), "recovered"], _registry(), [_oo()])

    (tool_msg,) = _tool_messages(result["messages"])
    assert tool_msg.status == "error"
    # E-1 unified envelope: an unclassified exception maps to INTERNAL; the message keeps the
    # pre-migration ``Error: ...`` wording verbatim (the compaction-checkpoint marker).
    assert json.loads(str(tool_msg.content)) == {
        "status": "error",
        "error_code": "INTERNAL",
        "message": "Error: RuntimeError: boom",
    }
    assert tool_msg.response_metadata["owc_error_code"] == "INTERNAL"
    assert tool_msg.response_metadata["owc_status"] == "error"
    assert result["messages"][-1].content == "recovered"


def test_skill_error_keeps_guided_wording() -> None:
    # echo without its required argument: the registry's SkillError becomes the observation.
    result = _run([("echo", {}), "ok"], _registry(), [_oo()])

    (tool_msg,) = _tool_messages(result["messages"])
    assert tool_msg.status == "error"
    # A missing-argument SkillError classifies as INVALID_ARG; the guided wording is preserved so
    # the model still sees exactly what to fix, now alongside a machine-readable code.
    envelope = json.loads(str(tool_msg.content))
    assert envelope["error_code"] == "INVALID_ARG"
    assert envelope["message"] == "Error: skill 'echo' is missing required argument(s): v"
    assert tool_msg.response_metadata["owc_error_code"] == "INVALID_ARG"


def test_success_observation_carries_ok_envelope_status() -> None:
    # The success exit is isomorphic to the error exit at the envelope level: every ToolMessage
    # carries owc_status, "ok" on success. The content stays the raw payload dump (byte parity),
    # so the payload IS the envelope's data — success is never re-wrapped.
    result = _run([("echo", {"v": "hi"}), "done"], _registry(), [_oo()])

    (tool_msg,) = _tool_messages(result["messages"])
    assert tool_msg.status == "success"
    assert tool_msg.response_metadata["owc_status"] == "ok"
    assert "owc_error_code" not in tool_msg.response_metadata
    assert str(tool_msg.content) == dumps_observation({"value": "hi"})


def test_oversized_observation_truncated_with_recoverable_marker() -> None:
    overflow: dict[str, str] = {}
    result = _run(
        [("big", {}), "done"],
        _registry(),
        [_oo(overflow=overflow, token_limit=50, char_cap=200)],
    )

    (tool_msg,) = _tool_messages(result["messages"])
    content = str(tool_msg.content)
    assert "[truncated: kept first" in content
    assert 'read_observation(obs_id="obs-0-0")' in content
    assert tool_msg.response_metadata["owc_is_truncated"] is True
    assert tool_msg.response_metadata["owc_observation_chars"] == len(content)
    # The FULL dump is recoverable through the production restore path.
    full = dumps_observation({"blob": "x" * 2000})
    assert overflow["obs-0-0"] == full
    with bind_overflow(overflow):
        assert read_observation("obs-0-0")["content"] == full


def test_within_limit_observation_is_byte_identical() -> None:
    overflow: dict[str, str] = {}
    result = _run([("echo", {"v": "hi"}), "done"], _registry(), [_oo(overflow=overflow)])

    (tool_msg,) = _tool_messages(result["messages"])
    assert str(tool_msg.content) == dumps_observation({"value": "hi"})
    assert tool_msg.response_metadata["owc_is_truncated"] is False
    assert overflow == {}  # no store write for an observation that fits


def test_obs_ids_unique_across_multiple_truncations() -> None:
    overflow: dict[str, str] = {}
    _run(
        [("big", {}), ("big", {}), "done"],
        _registry(),
        [_oo(overflow=overflow, token_limit=50, char_cap=200)],
    )

    assert set(overflow) == {"obs-0-0", "obs-1-1"}  # obs-{step}-{seq}, no collisions


def test_parallel_tool_calls_run_serially_under_the_run_lock() -> None:
    state = {"active": 0, "max_active": 0}
    gate = threading.Lock()

    def probe() -> dict[str, Any]:
        with gate:
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
        time.sleep(0.02)
        with gate:
            state["active"] -= 1
        return {"ok": True}

    registry = _registry(extra=[_skill("probe", probe)])
    result = _run([[("probe", {}), ("probe", {}), ("probe", {})], "done"], registry, [_oo()])

    assert len(_tool_messages(result["messages"])) == 3
    assert state["max_active"] == 1  # one turn's parallel tool_calls stayed serial


def test_step_latency_and_cost_attributed_from_records_since_snapshot() -> None:
    collector = TelemetryCollector()
    inner_record = CallRecord(
        task="skill_llm", tier="cheap", input_tokens=1000, output_tokens=50, latency_ms=123.0
    )

    def uses_llm() -> dict[str, Any]:
        collector.record(inner_record)  # an LLM-calling skill billing through the same collector
        return {"ok": True}

    registry = _registry(extra=[_skill("uses_llm", uses_llm)])
    handler = TelemetryCallbackHandler(collector, tier="cheap", model="m")
    result = _run(
        [("uses_llm", {}), ("echo", {"v": "x"}), "done"],
        registry,
        [_oo(collector=collector)],
        config={"callbacks": [handler]},
    )

    llm_msg, echo_msg = _tool_messages(result["messages"])
    # Only the records appended DURING the tool call count — planning calls (recorded by the
    # callback handler around each model turn) are excluded by the snapshot.
    assert llm_msg.response_metadata["owc_latency_ms"] == pytest.approx(123.0)
    assert llm_msg.response_metadata["owc_cost_usd"] == pytest.approx(inner_record.cost_usd)
    assert echo_msg.response_metadata["owc_latency_ms"] == 0.0
    assert echo_msg.response_metadata["owc_cost_usd"] == 0.0
    assert len(collector.records) > 1  # the planning calls really were recorded alongside


def test_execute_tool_spans_with_error_marking() -> None:
    tracer, exporter = build_test_tracer()
    _run([("echo", {"v": "x"}), ("boom", {}), "done"], _registry(), [_oo(tracer=tracer)])

    spans = [s for s in exporter.get_finished_spans() if s.name == "execute_tool"]
    assert [s.attributes["gen_ai.tool.name"] for s in spans] == ["echo", "boom"]
    assert [s.attributes["agent.step_idx"] for s in spans] == [0, 1]
    assert spans[0].status.status_code.name != "ERROR"
    assert spans[1].status.status_code.name == "ERROR"


# --------------------------------------------------------------------------- context compression


def _echo_turns(n: int) -> list[Any]:
    words = "alpha beta gamma delta epsilon zeta eta theta"
    return [("echo", {"v": f"{words} turn {i}"}) for i in range(n)]


def test_compression_under_budget_leaves_the_request_untouched() -> None:
    recorder = _RecordingModelMiddleware()
    compression = ContextCompressionMiddleware(
        chat_model=scripted_chat_model(["never used"]), token_budget=100_000
    )
    result = _run([*_echo_turns(2), "done"], _registry(), [compression, recorder])

    assert compression.context_compressions == 0
    _, last_view = recorder.requests[-1]
    # The last model call saw the full, unprojected history: goal + 2 rounds.
    assert len([m for m in last_view if isinstance(m, ToolMessage)]) == 2
    assert not [m for m in last_view if "[Summary of turns" in str(m.content)]
    assert len(result["messages"]) == 1 + 2 * 2 + 1


def test_compression_projects_view_but_never_rewrites_state() -> None:
    recorder = _RecordingModelMiddleware()
    compression = ContextCompressionMiddleware(
        chat_model=scripted_chat_model(["c1", "c2", "c3", "c4", "c5"]),
        token_budget=60,
        compression_threshold=0.5,
        keep_recent=1,
    )
    result = _run([*_echo_turns(4), "done"], _registry(), [compression, recorder])

    assert compression.context_compressions >= 1
    assert compression.post_compress_tokens < compression.pre_compress_tokens

    compressed_views = [
        view
        for _, view in recorder.requests
        if any(isinstance(m, HumanMessage) and "[Summary of turns" in str(m.content) for m in view)
    ]
    assert compressed_views, "no model call ever saw a compressed view"
    view = compressed_views[-1]
    # The leading goal message survives, and the marker is a HumanMessage — never a fabricated
    # assistant/tool pair.
    assert isinstance(view[0], HumanMessage) and "Goal:" in str(view[0].content)
    # Pair integrity: every ToolMessage in the view correlates to a preceding AIMessage tool_call.
    requested_ids: set[str] = set()
    for m in view:
        if isinstance(m, AIMessage):
            requested_ids.update(str(tc.get("id")) for tc in m.tool_calls)
        if isinstance(m, ToolMessage):
            assert m.tool_call_id in requested_ids, "orphan ToolMessage in compressed view"

    # State is append-only: the full history survives, with no summary marker anywhere.
    state_messages = result["messages"]
    assert len(state_messages) == 1 + 2 * 4 + 1
    assert not [m for m in state_messages if "[Summary of turns" in str(m.content)]


def test_compaction_call_is_tagged_compact_for_telemetry() -> None:
    collector = TelemetryCollector()
    handler = TelemetryCallbackHandler(collector, tier="cheap", model="m")
    compression = ContextCompressionMiddleware(
        chat_model=scripted_chat_model(["c1", "c2", "c3", "c4", "c5"]),
        token_budget=60,
        compression_threshold=0.5,
        keep_recent=1,
    )
    _run(
        [*_echo_turns(4), "done"],
        _registry(),
        [compression],
        config={"callbacks": [handler]},
    )

    tasks = {r.task for r in collector.records}
    assert "compact" in tasks  # the compaction call inherited callbacks and got its own label
    assert "agent_react" in tasks  # planning calls keep the default label


def test_checkpoint_error_round_is_preserved_verbatim() -> None:
    recorder = _RecordingModelMiddleware()
    compression = ContextCompressionMiddleware(
        chat_model=scripted_chat_model(["c1", "c2", "c3", "c4", "c5"]),
        token_budget=60,
        compression_threshold=0.5,
        keep_recent=1,
    )
    _run(
        [("boom", {}), *_echo_turns(3), "done"],
        _registry(),
        [compression, recorder, _oo()],
    )

    compressed_views = [
        view
        for _, view in recorder.requests
        if any("[Summary of turns" in str(m.content) for m in view)
    ]
    assert compressed_views
    # The error observation (a checkpoint round) must survive verbatim ahead of the marker.
    for view in compressed_views:
        error_messages = [m for m in view if isinstance(m, ToolMessage) and m.status == "error"]
        assert error_messages
        # The error round survives verbatim — the ``Error:`` lead-in inside the envelope message is
        # what marks it a checkpoint (context_compressor protects ``\bError\s*:`` turns).
        envelope = json.loads(str(error_messages[0].content))
        assert envelope["error_code"] == "INTERNAL"
        assert envelope["message"] == "Error: RuntimeError: boom"


# --------------------------------------------------------------------------- memory injection


def test_absent_reflections_leave_system_message_byte_identical() -> None:
    baseline = _RecordingModelMiddleware()
    agent = create_agent(
        scripted_chat_model(["done"]),
        as_structured_tools(_registry()),
        system_prompt="BASE PROMPT",
        middleware=[baseline],
    )
    agent.invoke({"messages": [HumanMessage("Goal: g")]})

    for reflections in (None, []):
        recorder = _RecordingModelMiddleware()
        agent = create_agent(
            scripted_chat_model(["done"]),
            as_structured_tools(_registry()),
            system_prompt="BASE PROMPT",
            middleware=[MemoryInjectionMiddleware(reflections), recorder],
        )
        agent.invoke({"messages": [HumanMessage("Goal: g")]})
        assert recorder.requests[0][0].content == baseline.requests[0][0].content == "BASE PROMPT"


def test_reflections_render_as_system_suffix_block() -> None:
    recorder = _RecordingModelMiddleware()
    agent = create_agent(
        scripted_chat_model(["done"]),
        as_structured_tools(_registry()),
        system_prompt="BASE PROMPT",
        middleware=[MemoryInjectionMiddleware(["watch the giver id", "audit first"]), recorder],
    )
    agent.invoke({"messages": [HumanMessage("Goal: g")]})

    assert recorder.requests[0][0].content == (
        "BASE PROMPT\n\nReflections from previous attempts:\n- watch the giver id\n- audit first"
    )


# --------------------------------------------------------------------------- budget finish


def _finish_fake(reply: str | None = None) -> _RecordingFinishFake:
    messages = [AIMessage(content=reply)] if reply is not None else []
    return _RecordingFinishFake(messages=iter(messages))


def _bf_run(script, *, run_limit, early_stopping, finish_model):
    limiter = ModelCallLimitMiddleware(run_limit=run_limit, exit_behavior="end")
    finish = BudgetFinishMiddleware(
        chat_model=finish_model,
        run_limit=run_limit,
        early_stopping=early_stopping,
        goal="test goal",
    )
    agent = create_agent(
        scripted_chat_model(script),
        as_structured_tools(_registry()),
        middleware=[limiter, finish],
    )
    return agent.invoke({"messages": [HumanMessage("Goal: test goal")]})


def test_normal_finish_reports_stop_reason_finished() -> None:
    finish_model = _finish_fake()
    result = _bf_run(
        [("echo", {"v": "x"}), "done"],
        run_limit=6,
        early_stopping="force",
        finish_model=finish_model,
    )

    assert result["owc_stop_reason"] == "finished"
    assert result["messages"][-1].content == "done"
    assert finish_model.seen == []  # no finish call on a normal run


def test_force_exhaustion_reports_max_steps_without_extra_call() -> None:
    finish_model = _finish_fake()
    result = _bf_run(
        [("echo", {"v": "1"}), ("echo", {"v": "2"})],
        run_limit=2,
        early_stopping="force",
        finish_model=finish_model,
    )

    assert result["owc_stop_reason"] == "max_steps"
    assert finish_model.seen == []  # force never spends the extra call
    assert str(result["messages"][-1].content).startswith("Model call limits exceeded")


def test_generate_exhaustion_spends_one_call_and_appends_honest_finish() -> None:
    finish_model = _finish_fake("Thought: t\nFinal Answer: honest wrap-up")
    # The limiter's 'end' jump lands on the after_agent chain — BudgetFinish still runs there.
    result = _bf_run(
        [("echo", {"v": "1"}), ("echo", {"v": "2"})],
        run_limit=2,
        early_stopping="generate",
        finish_model=finish_model,
    )

    assert result["owc_stop_reason"] == "max_steps_generate"
    assert result["messages"][-1].content == "honest wrap-up"
    assert len(finish_model.seen) == 1  # exactly ONE extra call
    system, user = finish_model.seen[0]
    assert "budget for this investigation is exhausted" in str(system.content)
    assert "Goal: test goal" in str(user.content)
    assert "The step budget above is exhausted" in str(user.content)


def test_generate_failure_falls_back_to_force_with_warning(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="owcopilot.agent.middleware"):
        result = _bf_run(
            [("echo", {"v": "1"}), ("echo", {"v": "2"})],
            run_limit=2,
            early_stopping="generate",
            finish_model=_RaisingFake(messages=iter([])),
        )

    assert result["owc_stop_reason"] == "max_steps"
    assert any("falling back to the 'force' stop" in r.message for r in caplog.records)


def test_finishing_exactly_at_the_budget_is_still_finished() -> None:
    # 2 model calls with run_limit=2 — but the 2nd call IS the final answer: not an exhaustion.
    finish_model = _finish_fake()
    result = _bf_run(
        [("echo", {"v": "x"}), "done"],
        run_limit=2,
        early_stopping="generate",
        finish_model=finish_model,
    )

    assert result["owc_stop_reason"] == "finished"
    assert result["messages"][-1].content == "done"
    assert finish_model.seen == []


def test_budget_finish_rejects_bad_early_stopping() -> None:
    with pytest.raises(ValueError, match="early_stopping"):
        BudgetFinishMiddleware(
            chat_model=_finish_fake(),
            run_limit=1,
            early_stopping="bogus",  # type: ignore[arg-type]
        )


# --------------------------------------------------------------------------- gen_ai.chat span


def test_gen_ai_chat_spans_carry_model_and_usage() -> None:
    tracer, exporter = build_test_tracer()
    usage_message = AIMessage(
        content="done",
        usage_metadata={
            "input_tokens": 11,
            "output_tokens": 7,
            "total_tokens": 18,
            "input_token_details": {},
        },
        response_metadata={"model_name": "deepseek-v4-flash-0716"},
    )

    class _Bindable(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    agent = create_agent(
        _Bindable(messages=iter([usage_message])),
        as_structured_tools(_registry()),
        middleware=[TelemetrySpanMiddleware(model="deepseek-v4-flash", tracer=tracer)],
    )
    agent.invoke({"messages": [HumanMessage("Goal: g")]})

    (span,) = [s for s in exporter.get_finished_spans() if s.name == "gen_ai.chat"]
    assert span.attributes["gen_ai.request.model"] == "deepseek-v4-flash"
    assert span.attributes["gen_ai.usage.input_tokens"] == 11
    assert span.attributes["gen_ai.usage.output_tokens"] == 7
    assert span.attributes["gen_ai.response.model"] == "deepseek-v4-flash-0716"
    assert span.attributes["agent.step_idx"] == 0


# --------------------------------------------------------------------------- composition order


class _WrapProbe(AgentMiddleware):
    def __init__(self, label: str, sink: list[str]) -> None:
        super().__init__()
        self.label = label
        self.sink = sink

    @property
    def name(self) -> str:  # create_agent rejects duplicate middleware names (class-name default)
        return f"WrapProbe_{self.label}"

    def wrap_model_call(self, request, handler):
        self.sink.append(f"{self.label}:model:in")
        response = handler(request)
        self.sink.append(f"{self.label}:model:out")
        return response

    def wrap_tool_call(self, request, handler):
        self.sink.append(f"{self.label}:tool:in")
        result = handler(request)
        self.sink.append(f"{self.label}:tool:out")
        return result


class _AfterProbe(AgentMiddleware):
    def __init__(self, label: str, sink: list[str]) -> None:
        super().__init__()
        self.label = label
        self.sink = sink

    @property
    def name(self) -> str:  # create_agent rejects duplicate middleware names (class-name default)
        return f"AfterProbe_{self.label}"

    def after_agent(self, state, runtime):
        self.sink.append(self.label)
        return None


def test_wrap_hooks_compose_first_in_list_as_outermost() -> None:
    sink: list[str] = []
    _run(
        [("echo", {"v": "x"}), "done"],
        _registry(),
        [_WrapProbe("A", sink), _WrapProbe("B", sink)],
    )

    tool_events = [e for e in sink if ":tool:" in e]
    assert tool_events == ["A:tool:in", "B:tool:in", "B:tool:out", "A:tool:out"]
    model_events = [e for e in sink if ":model:" in e]
    assert model_events[:2] == ["A:model:in", "B:model:in"]
    assert model_events[2:4] == ["B:model:out", "A:model:out"]


def test_after_agent_runs_in_reverse_declaration_order() -> None:
    sink: list[str] = []
    _run(
        ["done"],
        _registry(),
        [_AfterProbe("A", sink), _AfterProbe("B", sink)],
    )

    assert sink == ["B", "A"]  # first-in-list is outermost: its after_agent runs LAST
