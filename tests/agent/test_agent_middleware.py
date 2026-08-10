"""Self-built agent middleware on the create_agent host (T2-S4).

Per-middleware behaviour pins (error recovery, recoverable truncation, serial tool execution,
read-time compression that never touches state, byte-identical memory injection, the
force/generate budget finish and the ``owc_stop_reason`` tri-state) plus the composition-order
contracts: wrap hooks compose first-in-list-outermost, ``after_agent`` runs in reverse
declaration order, and a ``ModelCallLimitMiddleware`` 'end' jump still reaches
``BudgetFinishMiddleware``.
"""

from __future__ import annotations

import asyncio
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
    SettledCostBudget,
    TelemetrySpanMiddleware,
    _observation_hash,
    _reflections_block,
)
from owcopilot.fakes import scripted_chat_model
from owcopilot.llm.otel_bridge import build_test_tracer
from owcopilot.llm.telemetry import CallRecord, TelemetryCollector
from owcopilot.llm.telemetry_callback import TelemetryCallbackHandler
from owcopilot.skills import (
    CostTier,
    PollingPolicy,
    SideEffect,
    Skill,
    SkillParameter,
    SkillRegistry,
)
from owcopilot.skills.langchain_tools import as_structured_tools, dumps_observation
from owcopilot.skills.observation_overflow import bind_overflow, read_observation


def _billed_call(*, input_tokens: int, output_tokens: int = 0) -> CallRecord:
    return CallRecord(
        task="agent_react",
        tier="cheap",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def test_settled_cost_same_run_checkpoint_hydration_does_not_double_count(monkeypatch) -> None:
    monkeypatch.delenv("OWCOPILOT_PRICE_CHEAP", raising=False)
    collector = TelemetryCollector()
    gate = SettledCostBudget(1.0, collector=collector)

    collector.record(_billed_call(input_tokens=1_000_000))  # illustrative $0.30
    assert gate.settled_cost_usd == pytest.approx(0.30)
    assert gate.cost_is_estimate is True

    # The next graph turn hydrates the value this same live collector already contributes. It must
    # remain $0.30, not restored $0.30 + collector $0.30 = $0.60.
    gate.restore(0.30, cost_is_estimate=True)
    assert gate.settled_cost_usd == pytest.approx(0.30)

    collector.record(_billed_call(input_tokens=500_000))
    assert gate.settled_cost_usd == pytest.approx(0.45)
    gate.restore(0.45, cost_is_estimate=True)
    assert gate.settled_cost_usd == pytest.approx(0.45)


def test_settled_cost_resume_reanchors_new_collector_then_adds_new_calls(monkeypatch) -> None:
    monkeypatch.setenv("OWCOPILOT_PRICE_CHEAP", "0.006,0.30,1.20")
    resumed_collector = TelemetryCollector()
    gate = SettledCostBudget(
        1.0,
        collector=resumed_collector,
        restored_cost_usd=0.30,
        restored_cost_is_estimate=False,
    )

    assert gate.settled_cost_usd == pytest.approx(0.30)
    resumed_collector.record(_billed_call(input_tokens=500_000))
    assert gate.settled_cost_usd == pytest.approx(0.45)
    assert gate.cost_is_estimate is False  # explicit tier price, therefore real settled spend


@pytest.mark.parametrize("bad", [-1.0, float("nan"), float("inf")])
def test_settled_cost_gate_rejects_non_finite_or_negative_values(bad) -> None:
    with pytest.raises(ValueError, match="finite value >= 0"):
        SettledCostBudget(bad, collector=TelemetryCollector())

    gate = SettledCostBudget(1.0, collector=TelemetryCollector())
    with pytest.raises(ValueError, match="finite value >= 0"):
        gate.restore(bad)


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


def test_observation_hash_includes_status_artifact_and_state_markers() -> None:
    base = ToolMessage(content="same", tool_call_id="c", status="success", artifact={"v": 1})
    artifact_changed = ToolMessage(
        content="same", tool_call_id="c", status="success", artifact={"v": 2}
    )
    state_changed = ToolMessage(
        content="same", tool_call_id="c", status="success", artifact={"v": 1}
    )
    state_changed.response_metadata["owc_state_version"] = 2
    error = ToolMessage(content="same", tool_call_id="c", status="error", artifact={"v": 1})

    hashes = {_observation_hash(item) for item in (base, artifact_changed, state_changed, error)}
    assert len(hashes) == 4


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


def test_exact_no_progress_blocks_third_consecutive_same_action_and_result() -> None:
    calls: list[str] = []

    def stable(key: str) -> dict[str, str]:
        calls.append(key)
        return {"key": key, "value": "unchanged"}

    registry = _registry(
        [
            _skill(
                "stable",
                stable,
                (SkillParameter("key", "string", "key", required=True),),
            )
        ]
    )
    result = _run(
        [
            ("stable", {"key": "a"}),
            ("stable", {"key": "a"}),
            ("stable", {"key": "a"}),
            "done",
        ],
        registry,
        [_oo(registry=registry)],
    )

    messages = _tool_messages(result["messages"])
    assert calls == ["a", "a"]
    assert len(messages) == 3
    assert messages[-1].status == "error"
    assert json.loads(str(messages[-1].content))["error_code"] == "NO_PROGRESS"


def test_async_tool_wrapper_has_identical_no_progress_semantics() -> None:
    calls: list[str] = []
    registry = _registry(
        [
            _skill(
                "async_stable",
                lambda key: (calls.append(key), {"key": key, "value": "same"})[1],
                (SkillParameter("key", "string", "key", required=True),),
            )
        ]
    )
    agent = create_agent(
        scripted_chat_model([("async_stable", {"key": "a"})] * 3 + ["done"]),
        as_structured_tools(registry),
        middleware=[_oo(registry=registry)],
    )

    result = asyncio.run(agent.ainvoke({"messages": [HumanMessage("Goal: async parity")]}))

    messages = _tool_messages(result["messages"])
    assert calls == ["a", "a"]
    assert json.loads(str(messages[-1].content))["error_code"] == "NO_PROGRESS"


def test_a_b_a_is_not_a_consecutive_exact_loop() -> None:
    calls: list[str] = []

    def stable(key: str) -> dict[str, str]:
        calls.append(key)
        return {"key": key, "value": "unchanged"}

    registry = _registry(
        [
            _skill(
                "stable",
                stable,
                (SkillParameter("key", "string", "key", required=True),),
            )
        ]
    )
    result = _run(
        [
            ("stable", {"key": "a"}),
            ("stable", {"key": "b"}),
            ("stable", {"key": "a"}),
            "done",
        ],
        registry,
        [_oo(registry=registry)],
    )

    assert calls == ["a", "b", "a"]
    assert all(message.status == "success" for message in _tool_messages(result["messages"]))


@pytest.mark.parametrize("cycle", [("cycle_a", "cycle_b"), ("cycle_a", "cycle_b", "cycle_c")])
def test_completed_short_semantic_cycle_is_blocked_at_third_cycle_start(cycle) -> None:
    calls: list[str] = []

    def make_skill(name: str) -> Skill:
        return _skill(name, lambda: (calls.append(name), {"value": name})[1])

    registry = _registry([make_skill(name) for name in cycle])
    script = [(name, {}) for name in cycle * 2] + [(cycle[0], {}), "done"]
    result = _run(script, registry, [_oo(registry=registry)])

    messages = _tool_messages(result["messages"])
    assert calls == list(cycle * 2)
    assert all(message.status == "success" for message in messages[:-1])
    assert json.loads(str(messages[-1].content))["error_code"] == "NO_PROGRESS"


def test_same_tool_and_args_with_changing_observation_is_not_blocked() -> None:
    revisions: list[int] = []

    def changing(key: str) -> dict[str, Any]:
        revisions.append(len(revisions) + 1)
        return {"key": key, "revision": revisions[-1]}

    registry = _registry(
        [
            _skill(
                "changing",
                changing,
                (SkillParameter("key", "string", "key", required=True),),
            )
        ]
    )
    result = _run(
        [
            ("changing", {"key": "a"}),
            ("changing", {"key": "a"}),
            ("changing", {"key": "a"}),
            "done",
        ],
        registry,
        [_oo(registry=registry)],
    )

    messages = _tool_messages(result["messages"])
    assert revisions == [1, 2, 3]
    assert all(message.status == "success" for message in messages)


def test_skill_semantic_projection_ignores_volatile_runtime_fields() -> None:
    calls: list[int] = []

    def volatile() -> dict[str, Any]:
        calls.append(len(calls) + 1)
        return {"finding": "same", "audit_id": f"run-{calls[-1]}", "started_at": calls[-1]}

    registry = _registry(
        [
            Skill(
                name="volatile",
                description="stable finding with volatile audit metadata",
                cost_tier=CostTier.DETERMINISTIC,
                side_effect=SideEffect.READ_ONLY,
                handler=volatile,
                semantic_projection=lambda result: {"finding": result["finding"]},
                semantic_projection_name="test:stable-finding-v1",
                state_marker=lambda: "revision-1",
                state_marker_policy="test-revision-v1",
            )
        ]
    )
    result = _run(
        [("volatile", {})] * 3 + ["done"],
        registry,
        [_oo(registry=registry)],
    )

    messages = _tool_messages(result["messages"])
    assert calls == [1, 2]
    assert messages[0].artifact != messages[1].artifact  # full evidence remains untouched
    assert (
        messages[0].response_metadata["owc_semantic_observation_hash"]
        == messages[1].response_metadata["owc_semantic_observation_hash"]
    )
    assert messages[0].response_metadata["owc_semantic_projection"] == "test:stable-finding-v1"
    assert json.loads(str(messages[-1].content))["error_code"] == "NO_PROGRESS"


def test_changed_external_state_marker_invalidates_repeat_before_pre_kill() -> None:
    calls: list[int] = []
    # pre/post for executions 1 and 2 are rev-1; the third pre-check observes rev-2, modelling a
    # project update made after the checkpointed observations but before resume/execution.
    markers = iter(["rev-1", "rev-1", "rev-1", "rev-1", "rev-2", "rev-2"])
    registry = _registry(
        [
            Skill(
                name="revision_aware",
                description="state-aware stable read",
                cost_tier=CostTier.DETERMINISTIC,
                side_effect=SideEffect.READ_ONLY,
                handler=lambda: (calls.append(len(calls) + 1), {"finding": "same"})[1],
                semantic_projection=lambda result: result,
                semantic_projection_name="test:stable-v1",
                state_marker=lambda: next(markers),
                state_marker_policy="test-revision-v1",
            )
        ]
    )
    result = _run(
        [("revision_aware", {})] * 3 + ["done"],
        registry,
        [_oo(registry=registry)],
    )

    messages = _tool_messages(result["messages"])
    assert calls == [1, 2, 3]
    assert all(message.status == "success" for message in messages)
    assert messages[-1].response_metadata["owc_state_pre"] == "rev-2"


def test_execution_semantic_args_drop_nonce_apply_default_and_coerce() -> None:
    calls: list[tuple[int, str]] = []

    def normalised(n: int, mode: str) -> dict[str, Any]:
        calls.append((n, mode))
        return {"stable": True}

    registry = _registry(
        [
            _skill(
                "normalised",
                normalised,
                (
                    SkillParameter("n", "integer", "number", required=True),
                    SkillParameter("mode", "string", "mode", default="safe"),
                ),
            )
        ]
    )
    result = _run(
        [
            ("normalised", {"n": "1", "nonce": "first"}),
            ("normalised", {"n": 1, "mode": "safe", "nonce": "second"}),
            ("normalised", {"n": "1", "nonce": "third"}),
            "done",
        ],
        registry,
        [_oo(registry=registry)],
    )

    assert calls == [(1, "safe"), (1, "safe")]
    envelope = json.loads(str(_tool_messages(result["messages"])[-1].content))
    assert envelope["error_code"] == "NO_PROGRESS"


def test_polling_policy_allows_pending_pending_ready() -> None:
    states = iter(["pending", "pending", "ready"])
    calls: list[str] = []

    def poll(job_id: str) -> dict[str, str]:
        calls.append(job_id)
        return {"job_id": job_id, "status": next(states)}

    registry = _registry(
        [
            Skill(
                name="poll",
                description="poll skill",
                cost_tier=CostTier.DETERMINISTIC,
                side_effect=SideEffect.READ_ONLY,
                handler=poll,
                parameters=(SkillParameter("job_id", "string", "job", required=True),),
                polling=PollingPolicy(max_polls=3, deadline_seconds=30),
            )
        ]
    )
    result = _run(
        [("poll", {"job_id": "j1"})] * 3 + ["done"],
        registry,
        [_oo(registry=registry)],
    )

    messages = _tool_messages(result["messages"])
    assert calls == ["j1", "j1", "j1"]
    assert all(message.status == "success" for message in messages)
    assert messages[0].response_metadata["owc_poll_pending"] is True
    assert messages[1].response_metadata["owc_poll_count"] == 2
    assert "owc_poll_pending" not in messages[2].response_metadata


def test_polling_policy_enforces_max_polls_with_typed_budget_error() -> None:
    calls: list[str] = []

    def pending(job_id: str) -> dict[str, str]:
        calls.append(job_id)
        return {"job_id": job_id, "status": "pending"}

    registry = _registry(
        [
            Skill(
                name="poll",
                description="poll skill",
                cost_tier=CostTier.DETERMINISTIC,
                side_effect=SideEffect.READ_ONLY,
                handler=pending,
                parameters=(SkillParameter("job_id", "string", "job", required=True),),
                polling=PollingPolicy(max_polls=2, deadline_seconds=30),
            )
        ]
    )
    result = _run(
        [("poll", {"job_id": "j1"})] * 3 + ["done"],
        registry,
        [_oo(registry=registry)],
    )

    messages = _tool_messages(result["messages"])
    assert calls == ["j1", "j1"]
    assert json.loads(str(messages[-1].content))["error_code"] == "BUDGET_EXCEEDED"
    assert messages[-1].response_metadata["owc_error_code"] == "BUDGET_EXCEEDED"


def test_polling_policy_enforces_deadline_before_next_execution() -> None:
    calls: list[str] = []
    ticks = iter([100.0, 131.0])

    def pending(job_id: str) -> dict[str, str]:
        calls.append(job_id)
        return {"job_id": job_id, "status": "pending"}

    registry = _registry(
        [
            Skill(
                name="poll",
                description="poll skill",
                cost_tier=CostTier.DETERMINISTIC,
                side_effect=SideEffect.READ_ONLY,
                handler=pending,
                parameters=(SkillParameter("job_id", "string", "job", required=True),),
                polling=PollingPolicy(max_polls=5, deadline_seconds=30),
            )
        ]
    )
    result = _run(
        [("poll", {"job_id": "j1"})] * 2 + ["done"],
        registry,
        [_oo(registry=registry, clock=lambda: next(ticks))],
    )

    messages = _tool_messages(result["messages"])
    assert calls == ["j1"]
    envelope = json.loads(str(messages[-1].content))
    assert envelope["error_code"] == "BUDGET_EXCEEDED"
    assert "deadline" in envelope["message"]


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
    pytest.importorskip("opentelemetry.sdk.trace", reason="opentelemetry-sdk not installed")
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

    content = recorder.requests[0][0].content
    assert content == "BASE PROMPT\n\n" + _reflections_block(
        ["watch the giver id", "audit first"]
    )
    assert "UNTRUSTED_MEMORY_JSON" in content


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
    pytest.importorskip("opentelemetry.sdk.trace", reason="opentelemetry-sdk not installed")
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
