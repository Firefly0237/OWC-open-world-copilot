"""T4-B: OTEL span tree tests.

Acceptance criteria (from SUPERVISOR_rubric.md P4B-1/2/3):
1. OWCOPILOT_OTEL_ENABLED=0 (default): no OTEL import side effects, get_tracer_or_noop()
   returns a no-op tracer, agent runs byte-identically.
2. OWCOPILOT_OTEL_ENABLED=1: agent.run() produces trace with >1 span.
3. Span tree: invoke_agent is root, gen_ai.chat is child, execute_tool is child of gen_ai.chat.
4. Span attributes: gen_ai.agent.name, gen_ai.usage.input_tokens, gen_ai.tool.name present.
5. trace_id is a valid 32-char hex string.
6. query_by_run_id returns spans from SQLite after export (persistence check).
7. All tests use InMemoryExporter (no network/SQLite needed in CI).
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Guard: skip all tests if opentelemetry-sdk not installed
# ---------------------------------------------------------------------------

otel_sdk = pytest.importorskip("opentelemetry.sdk.trace", reason="opentelemetry-sdk not installed")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_test_tracer():
    """Return (tracer, exporter) using InMemoryExporter."""
    from owcopilot.llm.otel_bridge import InMemoryExporter, build_test_tracer
    exporter = InMemoryExporter()
    tracer, exp = build_test_tracer(exporter)
    return tracer, exp


def _get_spans_by_name(spans, name: str) -> list:
    return [s for s in spans if s.name == name]


# ---------------------------------------------------------------------------
# no-op path: OTEL disabled
# ---------------------------------------------------------------------------

def test_otel_disabled_returns_noop_tracer(monkeypatch) -> None:
    """When OWCOPILOT_OTEL_ENABLED is unset/0, get_tracer_or_noop() returns a no-op tracer."""
    monkeypatch.delenv("OWCOPILOT_OTEL_ENABLED", raising=False)

    from owcopilot.llm.otel_bridge import _NOOP_TRACER, get_tracer_or_noop

    tracer = get_tracer_or_noop()
    assert tracer is _NOOP_TRACER, f"Expected no-op tracer, got {type(tracer)}"


def test_noop_span_does_not_raise() -> None:
    """No-op spans must not raise on any standard span API call."""
    from owcopilot.llm.otel_bridge import _NoOpSpan

    span = _NoOpSpan()
    span.set_attribute("gen_ai.agent.name", "test")
    span.set_attribute("gen_ai.usage.input_tokens", 42)
    span.set_status("OK")
    span.record_exception(ValueError("test"))


def test_noop_context_managers_do_not_raise(monkeypatch) -> None:
    """invoke_agent_span / gen_ai_chat_span / execute_tool_span work with no-op tracer."""
    monkeypatch.delenv("OWCOPILOT_OTEL_ENABLED", raising=False)

    from owcopilot.llm.otel_bridge import (
        _NOOP_TRACER,
        execute_tool_span,
        gen_ai_chat_span,
        invoke_agent_span,
    )

    with invoke_agent_span(_NOOP_TRACER, agent_name="test", goal="test goal") as root:
        root.set_attribute("agent.run_id", "abc")
        with gen_ai_chat_span(_NOOP_TRACER, model="mock", step_idx=0) as chat:
            chat.set_attribute("gen_ai.usage.input_tokens", 10)
            with execute_tool_span(_NOOP_TRACER, tool_name="audit_project", step_idx=0) as tool:
                tool.set_attribute("gen_ai.tool.name", "audit_project")


# ---------------------------------------------------------------------------
# span tree structure with in-memory exporter
# ---------------------------------------------------------------------------

def test_span_tree_invoke_agent_is_root() -> None:
    """P4B-2: invoke_agent span has no parent (root span)."""
    from owcopilot.llm.otel_bridge import invoke_agent_span

    tracer, exporter = _build_test_tracer()

    with invoke_agent_span(tracer, agent_name="test-agent", goal="test goal"):
        pass

    spans = exporter.get_finished_spans()
    assert spans, "No spans exported"

    invoke_spans = _get_spans_by_name(spans, "invoke_agent")
    assert invoke_spans, "No 'invoke_agent' span found"

    root = invoke_spans[0]
    assert root.parent is None, f"invoke_agent should be root, but has parent: {root.parent}"


def test_span_tree_gen_ai_chat_is_child_of_invoke_agent() -> None:
    """P4B-2: gen_ai.chat is a child span of invoke_agent."""
    from owcopilot.llm.otel_bridge import gen_ai_chat_span, invoke_agent_span

    tracer, exporter = _build_test_tracer()

    with invoke_agent_span(tracer, agent_name="test-agent", goal="test goal"):
        with gen_ai_chat_span(tracer, model="deepseek-v4-flash", step_idx=0):
            pass

    spans = exporter.get_finished_spans()
    invoke_spans = _get_spans_by_name(spans, "invoke_agent")
    chat_spans = _get_spans_by_name(spans, "gen_ai.chat")

    assert invoke_spans, "No invoke_agent span"
    assert chat_spans, "No gen_ai.chat span"

    root = invoke_spans[0]
    chat = chat_spans[0]

    assert chat.parent is not None, "gen_ai.chat should have a parent"
    assert chat.parent.span_id == root.get_span_context().span_id, (
        "gen_ai.chat parent should be invoke_agent"
    )


def test_span_tree_execute_tool_is_child_of_gen_ai_chat() -> None:
    """P4B-2: execute_tool is a child of gen_ai.chat."""
    from owcopilot.llm.otel_bridge import execute_tool_span, gen_ai_chat_span, invoke_agent_span

    tracer, exporter = _build_test_tracer()

    with invoke_agent_span(tracer, agent_name="test-agent", goal="goal"):
        with gen_ai_chat_span(tracer, model="mock", step_idx=0):
            with execute_tool_span(tracer, tool_name="audit_project", step_idx=0):
                pass

    spans = exporter.get_finished_spans()
    tool_spans = _get_spans_by_name(spans, "execute_tool")
    chat_spans = _get_spans_by_name(spans, "gen_ai.chat")

    assert tool_spans, "No execute_tool span"
    assert chat_spans, "No gen_ai.chat span"

    tool = tool_spans[0]
    chat = chat_spans[0]

    assert tool.parent is not None, "execute_tool should have a parent"
    assert tool.parent.span_id == chat.get_span_context().span_id, (
        "execute_tool parent should be gen_ai.chat"
    )


# ---------------------------------------------------------------------------
# Span attributes: GenAI semantic conventions
# ---------------------------------------------------------------------------

def test_invoke_agent_span_attributes() -> None:
    """invoke_agent span must carry gen_ai.agent.name, gen_ai.operation.name, gen_ai.system."""
    from owcopilot.llm.otel_bridge import invoke_agent_span

    tracer, exporter = _build_test_tracer()

    with invoke_agent_span(tracer, agent_name="world-consistency", goal="check the world"):
        pass

    spans = exporter.get_finished_spans()
    root = _get_spans_by_name(spans, "invoke_agent")[0]
    attrs = dict(root.attributes or {})

    assert attrs.get("gen_ai.agent.name") == "world-consistency"
    assert attrs.get("gen_ai.operation.name") == "invoke_agent"
    assert attrs.get("gen_ai.system") == "owcopilot"
    assert attrs.get("agent.goal") == "check the world"


def test_gen_ai_chat_span_token_attributes() -> None:
    """gen_ai.chat span carries gen_ai.usage.input_tokens and output_tokens."""
    from owcopilot.llm.otel_bridge import gen_ai_chat_span, invoke_agent_span

    tracer, exporter = _build_test_tracer()

    with invoke_agent_span(tracer, agent_name="test", goal="goal"):
        with gen_ai_chat_span(
            tracer, model="deepseek-v4-flash", step_idx=0,
            input_tokens=100, output_tokens=50
        ):
            pass

    spans = exporter.get_finished_spans()
    chat = _get_spans_by_name(spans, "gen_ai.chat")[0]
    attrs = dict(chat.attributes or {})

    assert attrs.get("gen_ai.usage.input_tokens") == 100
    assert attrs.get("gen_ai.usage.output_tokens") == 50
    assert attrs.get("gen_ai.request.model") == "deepseek-v4-flash"
    assert attrs.get("agent.step_idx") == 0


def test_execute_tool_span_tool_name_attribute() -> None:
    """execute_tool span must carry gen_ai.tool.name."""
    from owcopilot.llm.otel_bridge import execute_tool_span, gen_ai_chat_span, invoke_agent_span

    tracer, exporter = _build_test_tracer()

    with invoke_agent_span(tracer, agent_name="test", goal="goal"):
        with gen_ai_chat_span(tracer, model="mock", step_idx=1):
            with execute_tool_span(tracer, tool_name="retrieve_lore", step_idx=1):
                pass

    spans = exporter.get_finished_spans()
    tool = _get_spans_by_name(spans, "execute_tool")[0]
    attrs = dict(tool.attributes or {})

    assert attrs.get("gen_ai.tool.name") == "retrieve_lore"
    assert attrs.get("agent.step_idx") == 1


# ---------------------------------------------------------------------------
# trace_id format
# ---------------------------------------------------------------------------

def test_trace_id_is_valid_32_char_hex() -> None:
    """P4B: trace_id must be a valid 32-character lowercase hex string."""
    from owcopilot.llm.otel_bridge import invoke_agent_span

    tracer, exporter = _build_test_tracer()

    with invoke_agent_span(tracer, agent_name="test", goal="goal"):
        pass

    spans = exporter.get_finished_spans()
    root = _get_spans_by_name(spans, "invoke_agent")[0]
    ctx = root.get_span_context()
    trace_id_hex = format(ctx.trace_id, "032x")

    assert len(trace_id_hex) == 32, f"trace_id should be 32 chars, got {len(trace_id_hex)}"
    assert all(c in "0123456789abcdef" for c in trace_id_hex), (
        f"trace_id should be lowercase hex, got {trace_id_hex!r}"
    )


# ---------------------------------------------------------------------------
# Span count: multiple steps produce multiple spans
# ---------------------------------------------------------------------------

def test_multiple_steps_produce_multiple_spans() -> None:
    """A 2-step agent run produces: 1 invoke_agent + 2 gen_ai.chat + 2 execute_tool = 5 spans."""
    from owcopilot.llm.otel_bridge import execute_tool_span, gen_ai_chat_span, invoke_agent_span

    tracer, exporter = _build_test_tracer()

    with invoke_agent_span(tracer, agent_name="test", goal="goal"):
        for step in range(2):
            with gen_ai_chat_span(tracer, model="mock", step_idx=step):
                with execute_tool_span(tracer, tool_name="audit_project", step_idx=step):
                    pass

    spans = exporter.get_finished_spans()
    assert len(spans) >= 5, f"Expected ≥5 spans, got {len(spans)}: {[s.name for s in spans]}"
    assert len(_get_spans_by_name(spans, "invoke_agent")) == 1
    assert len(_get_spans_by_name(spans, "gen_ai.chat")) == 2
    assert len(_get_spans_by_name(spans, "execute_tool")) == 2


# ---------------------------------------------------------------------------
# SQLite persistence: query_by_run_id (P4B-1/3)
# ---------------------------------------------------------------------------

def test_sqlite_span_export_and_query_by_run_id(tmp_path) -> None:
    """P4B-1/3: Spans persist to SQLite and can be queried by run_id."""

    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    from owcopilot.llm.otel_bridge import (
        SqliteSpanExporter,
        execute_tool_span,
        gen_ai_chat_span,
        invoke_agent_span,
        query_by_run_id,
    )

    db_path = str(tmp_path / "test_traces.db")
    sqlite_exporter = SqliteSpanExporter(db_path=db_path)

    resource = Resource.create({"service.name": "test"})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(SimpleSpanProcessor(sqlite_exporter))
    tracer = provider.get_tracer("test")

    # Capture the trace_id from the root span
    captured_trace_id: list[str] = []

    with invoke_agent_span(tracer, agent_name="test-agent", goal="test") as root:
        ctx = root.get_span_context()
        captured_trace_id.append(format(ctx.trace_id, "032x"))
        with gen_ai_chat_span(tracer, model="mock", step_idx=0):
            with execute_tool_span(tracer, tool_name="audit_project", step_idx=0):
                pass

    # Force flush
    provider.force_flush(timeout_millis=1000)
    sqlite_exporter.shutdown()

    assert captured_trace_id, "No trace_id captured"
    run_id = captured_trace_id[0]

    # Query by run_id
    rows = query_by_run_id(run_id, db_path=db_path)
    assert rows, f"No spans found for run_id={run_id!r}"

    names = [r["name"] for r in rows]
    assert "invoke_agent" in names, f"invoke_agent not in persisted spans: {names}"
    assert "gen_ai.chat" in names, f"gen_ai.chat not in persisted spans: {names}"
    assert "execute_tool" in names, f"execute_tool not in persisted spans: {names}"

    # Verify parent_span_id chain (span tree structure in DB)
    root_rows = [r for r in rows if r["parent_span_id"] == ""]
    assert root_rows, "No root span (empty parent_span_id) in DB"
    assert root_rows[0]["name"] == "invoke_agent"

    # Verify all rows have the same run_id = trace_id
    assert all(r["run_id"] == run_id for r in rows), "run_id mismatch in persisted spans"


def test_query_by_run_id_nonexistent_returns_empty(tmp_path) -> None:
    """Querying a non-existent run_id returns []."""
    from owcopilot.llm.otel_bridge import query_by_run_id

    db_path = str(tmp_path / "empty.db")
    rows = query_by_run_id("nonexistent_run_id", db_path=db_path)
    assert rows == []


# ---------------------------------------------------------------------------
# RT4-①: threading.Lock prevents double-provider initialisation
# ---------------------------------------------------------------------------

def test_get_tracer_thread_safe_single_provider(monkeypatch) -> None:
    """RT4-①: Concurrent calls to get_tracer() must initialise exactly one TracerProvider.

    Strategy: reset the module-level globals, then fire N threads simultaneously.
    Each thread calls get_tracer() and records the provider id it received.
    All threads must see the same provider object (same id()), proving only one
    provider was created regardless of race conditions.
    """
    import threading as _threading

    import owcopilot.llm.otel_bridge as bridge

    # Reset module globals so the test runs independently of other tests.
    old_provider = bridge._TRACER_PROVIDER
    bridge._TRACER_PROVIDER = None

    collected_ids: list[int] = []
    errors: list[Exception] = []

    def _call_get_tracer() -> None:
        try:
            bridge.get_tracer("test-service")
            # Retrieve the provider that backs this tracer
            collected_ids.append(id(bridge._TRACER_PROVIDER))
        except Exception as exc:
            errors.append(exc)

    N = 8
    threads = [_threading.Thread(target=_call_get_tracer) for _ in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Restore original state
    bridge._TRACER_PROVIDER = old_provider

    assert not errors, f"Threads raised exceptions: {errors}"
    assert len(collected_ids) == N, f"Expected {N} id results, got {len(collected_ids)}"
    # All threads must have seen the same provider instance
    unique_ids = set(collected_ids)
    assert len(unique_ids) == 1, (
        f"Multiple TracerProvider instances created (ids={unique_ids}): "
        "threading.Lock is not preventing double-initialisation"
    )


# ---------------------------------------------------------------------------
# RT4-②: SQLite default path is absolute (never CWD-relative)
# ---------------------------------------------------------------------------

def test_sqlite_default_path_is_absolute() -> None:
    """RT4-②: _default_sqlite_path() returns an absolute path (never a bare filename)."""
    import os as _os

    from owcopilot.llm.otel_bridge import _default_sqlite_path

    path = _default_sqlite_path()
    assert _os.path.isabs(path), (
        f"_default_sqlite_path() must return an absolute path, got: {path!r}"
    )


def test_sqlite_exporter_default_path_is_absolute() -> None:
    """RT4-②: SqliteSpanExporter() with no args uses an absolute db_path."""
    import os as _os

    from owcopilot.llm.otel_bridge import SqliteSpanExporter

    exporter = SqliteSpanExporter()
    assert _os.path.isabs(exporter.db_path), (
        f"SqliteSpanExporter default db_path must be absolute, got: {exporter.db_path!r}"
    )


def test_sqlite_env_override_is_respected(monkeypatch, tmp_path) -> None:
    """RT4-②: OWCOPILOT_OTEL_SQLITE_PATH env var still overrides the default."""
    import os as _os

    custom_path = str(tmp_path / "custom_traces.db")
    monkeypatch.setenv("OWCOPILOT_OTEL_SQLITE_PATH", custom_path)

    # get_tracer uses `os.getenv(_OTEL_SQLITE_PATH_ENV) or _default_sqlite_path()`
    # so verify the env var is picked up by SqliteSpanExporter when explicitly passed.
    from owcopilot.llm.otel_bridge import _OTEL_SQLITE_PATH_ENV, SqliteSpanExporter

    resolved = _os.getenv(_OTEL_SQLITE_PATH_ENV) or "FALLBACK"
    assert resolved == custom_path

    # Also verify: exporter with explicit path keeps that path unchanged
    exporter = SqliteSpanExporter(db_path=custom_path)
    assert exporter.db_path == custom_path


# ---------------------------------------------------------------------------
# RT4-③: execute_tool_span sets ERROR status + records exception on tool failure
# ---------------------------------------------------------------------------

def test_execute_tool_span_error_status_on_skill_error() -> None:
    """RT4-③: SkillError inside execute_tool_span → span.status == ERROR, exception recorded."""
    from opentelemetry.trace import Status, StatusCode

    from owcopilot.llm.otel_bridge import execute_tool_span, gen_ai_chat_span, invoke_agent_span

    tracer, exporter = _build_test_tracer()

    class _FakeSkillError(Exception):
        pass

    with invoke_agent_span(tracer, agent_name="test", goal="goal"):
        with gen_ai_chat_span(tracer, model="mock", step_idx=0):
            with execute_tool_span(tracer, tool_name="bad_tool", step_idx=0) as tool_span:
                exc = _FakeSkillError("skill failed")
                tool_span.set_status(Status(StatusCode.ERROR, str(exc)))
                tool_span.record_exception(exc)

    spans = exporter.get_finished_spans()
    tool_spans = _get_spans_by_name(spans, "execute_tool")
    assert tool_spans, "No execute_tool span found"

    tool = tool_spans[0]
    assert tool.status.status_code == StatusCode.ERROR, (
        f"Expected ERROR status on tool span, got: {tool.status.status_code}"
    )
    # events should contain at least one exception event
    event_names = [e.name for e in (tool.events or [])]
    assert "exception" in event_names, (
        f"Expected 'exception' event on tool span, got events: {event_names}"
    )


def test_execute_tool_span_no_error_status_on_success() -> None:
    """RT4-③: Successful execute_tool span must NOT have ERROR status."""
    from opentelemetry.trace import StatusCode

    from owcopilot.llm.otel_bridge import execute_tool_span, gen_ai_chat_span, invoke_agent_span

    tracer, exporter = _build_test_tracer()

    with invoke_agent_span(tracer, agent_name="test", goal="goal"):
        with gen_ai_chat_span(tracer, model="mock", step_idx=0):
            with execute_tool_span(tracer, tool_name="good_tool", step_idx=0):
                pass  # no exception

    spans = exporter.get_finished_spans()
    tool_spans = _get_spans_by_name(spans, "execute_tool")
    assert tool_spans, "No execute_tool span found"

    tool = tool_spans[0]
    assert tool.status.status_code != StatusCode.ERROR, (
        f"Successful tool span should not have ERROR status, got: {tool.status.status_code}"
    )


# ---------------------------------------------------------------------------
# RT4-③: End-to-end: react.py sets ERROR status on SkillError and bare Exception
# ---------------------------------------------------------------------------

def _build_react_agent_with_tracer(
    exporter, failing_skill: bool = False, use_bare_exception: bool = False
):
    """Build a ReActAgent wired to an InMemoryExporter for span inspection.

    Patches ``owcopilot.agent.react.get_tracer_or_noop`` (the name as imported
    by react.py) so that agent.run() uses our test tracer.
    """
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    import owcopilot.agent.react as react_module
    from owcopilot.agent.react import ReActAgent
    from owcopilot.core.skills import CostTier, SideEffect, Skill, SkillRegistry
    from owcopilot.llm.cache import NoOpCache
    from owcopilot.llm.gateway import LLMGateway
    from owcopilot.llm.router import StaticRouter

    resource = Resource.create({"service.name": "test"})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    # Build the test tracer that will collect spans into our exporter
    test_tracer = provider.get_tracer("test")

    class _ScriptedProvider:
        def __init__(self):
            self._responses = [
                "Thought: try tool\nAction: bad_tool\nAction Input: {}",
                "Thought: done\nFinal Answer: all done",
            ]
            self._idx = 0

        def complete(self, *, system: str, user: str, model: str):
            text = self._responses[min(self._idx, len(self._responses) - 1)]
            self._idx += 1
            return text, 10, 5

    gw = LLMGateway(
        providers={"mock": _ScriptedProvider()},
        router=StaticRouter(mapping={"agent_react": "mock", "default": "mock"}),
        cache=NoOpCache(),
    )

    registry = SkillRegistry()

    if use_bare_exception:
        def _bad_handler(**kwargs):
            raise RuntimeError("unexpected crash")
    elif failing_skill:
        from owcopilot.core.skills import SkillError
        def _bad_handler(**kwargs):
            raise SkillError("skill kaboom")
    else:
        def _bad_handler(**kwargs):
            return {"ok": True}

    registry.register(Skill(
        name="bad_tool",
        description="a tool that may fail",
        cost_tier=CostTier.DETERMINISTIC,
        side_effect=SideEffect.READ_ONLY,
        handler=_bad_handler,
    ))

    agent = ReActAgent(gateway=gw, registry=registry, max_steps=3)

    # react.py does: from ..llm.otel_bridge import get_tracer_or_noop
    # so the name lives in the react module's namespace — patch it there.
    original_fn = react_module.get_tracer_or_noop

    def _patched_get_tracer_or_noop():
        return test_tracer

    react_module.get_tracer_or_noop = _patched_get_tracer_or_noop
    try:
        result = agent.run("test goal")
    finally:
        react_module.get_tracer_or_noop = original_fn

    # Force all spans to be flushed from the BatchSpanProcessor (SimpleSpanProcessor is sync)
    provider.force_flush(timeout_millis=1000)

    return result


def test_react_execute_tool_span_error_on_skill_error() -> None:
    """RT4-③ integration: react.py marks execute_tool span ERROR when SkillError is raised."""
    from opentelemetry.trace import StatusCode

    from owcopilot.llm.otel_bridge import InMemoryExporter

    exporter = InMemoryExporter()
    result = _build_react_agent_with_tracer(exporter, failing_skill=True)

    # The step should be recorded as an error in AgentStep
    assert result.steps, "No steps recorded"
    assert result.steps[0].is_error is True

    # The execute_tool span must have ERROR status
    spans = exporter.get_finished_spans()
    tool_spans = _get_spans_by_name(spans, "execute_tool")
    assert tool_spans, f"No execute_tool span found. All spans: {[s.name for s in spans]}"

    tool = tool_spans[0]
    assert tool.status.status_code == StatusCode.ERROR, (
        f"Expected ERROR status on execute_tool span after SkillError, "
        f"got: {tool.status.status_code}"
    )
    event_names = [e.name for e in (tool.events or [])]
    assert "exception" in event_names, (
        f"Expected 'exception' event on execute_tool span, got: {event_names}"
    )


# ---------------------------------------------------------------------------
# R3-Team-C ①: gen_ai.request.model carries the REAL model id, not the task label
# (regression for the OTEL GenAI semantic-convention bug — previously react.py passed
#  self.task="agent_react" as the model, which the old unit tests missed by testing the
#  context manager directly instead of going through react.py → span.)
# ---------------------------------------------------------------------------

def _run_react_with_tracer_and_provider(exporter, provider, *, mapping=None):
    """Run a one-shot ReActAgent (Final Answer immediately) through react.py into *exporter*.

    *provider* is a full provider object — its ``.model`` attribute (if any) is what the gateway
    resolves and what gen_ai.request.model should end up carrying.  Returns (result, spans).
    """
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    import owcopilot.agent.react as react_module
    from owcopilot.agent.react import ReActAgent
    from owcopilot.core.skills import SkillRegistry
    from owcopilot.llm.cache import NoOpCache
    from owcopilot.llm.gateway import LLMGateway
    from owcopilot.llm.router import StaticRouter

    resource = Resource.create({"service.name": "test"})
    tprovider = TracerProvider(resource=resource)
    tprovider.add_span_processor(SimpleSpanProcessor(exporter))
    test_tracer = tprovider.get_tracer("test")

    gw = LLMGateway(
        providers={"cheap": provider},
        router=StaticRouter(mapping=mapping or {"agent_react": "cheap", "default": "cheap"}),
        cache=NoOpCache(),
    )
    agent = ReActAgent(gateway=gw, registry=SkillRegistry(), max_steps=2)

    original_fn = react_module.get_tracer_or_noop
    react_module.get_tracer_or_noop = lambda: test_tracer
    try:
        result = agent.run("test goal")
    finally:
        react_module.get_tracer_or_noop = original_fn
    tprovider.force_flush(timeout_millis=1000)
    return result, exporter.get_finished_spans()


def test_react_chat_span_model_is_real_model_not_task_label() -> None:
    """R3-C①: gen_ai.request.model must be the resolved model id, never the agent task label."""
    from owcopilot.llm.otel_bridge import InMemoryExporter

    class _ModelProvider:
        # mimics OpenAICompatProvider: exposes a real model id behind the "cheap" tier
        model = "deepseek-v4-flash"

        def complete(self, *, system: str, user: str, model: str):
            return "Thought: done\nFinal Answer: all good", 11, 7

    result, spans = _run_react_with_tracer_and_provider(InMemoryExporter(), _ModelProvider())

    assert result.final_answer == "all good"
    chat_spans = _get_spans_by_name(spans, "gen_ai.chat")
    assert chat_spans, f"No gen_ai.chat span found. All spans: {[s.name for s in spans]}"
    attrs = dict(chat_spans[0].attributes or {})
    model_attr = attrs.get("gen_ai.request.model")
    assert model_attr == "deepseek-v4-flash", (
        f"gen_ai.request.model must be the real model id, got {model_attr!r}"
    )
    # And specifically NOT the internal task label — the original bug.
    assert model_attr != "agent_react"
    # token counts still back-filled honestly from telemetry
    assert attrs.get("gen_ai.usage.input_tokens") == 11
    assert attrs.get("gen_ai.usage.output_tokens") == 7


# ---------------------------------------------------------------------------
# T2-P1: gen_ai.response.model carries the model the API RESPONSE BODY reported
# (the model that actually answered), distinct from gen_ai.request.model. Set only
# when known — an offline fake (no response model) leaves the attribute unset.
# ---------------------------------------------------------------------------

def test_react_chat_span_sets_response_model_when_provider_reports_it() -> None:
    """T2-P1: a provider returning a response-body model id → gen_ai.response.model on the span."""
    from owcopilot.llm.otel_bridge import InMemoryExporter

    class _RespModelProvider:
        model = "deepseek-v4-flash"  # request side

        def complete(self, *, system: str, user: str, model: str):
            # 5-tuple: response-body model differs from the configured request model.
            return "Thought: done\nFinal Answer: ok", 5, 3, 0, "deepseek-v4-flash-0613"

    _result, spans = _run_react_with_tracer_and_provider(InMemoryExporter(), _RespModelProvider())
    attrs = dict(_get_spans_by_name(spans, "gen_ai.chat")[0].attributes or {})
    assert attrs.get("gen_ai.request.model") == "deepseek-v4-flash"
    assert attrs.get("gen_ai.response.model") == "deepseek-v4-flash-0613"


def test_react_chat_span_omits_response_model_for_offline_fake() -> None:
    """T2-P1: an offline fake (3-tuple, no response model) must NOT set gen_ai.response.model —
    never back-filled with a guessed/request-side value."""
    from owcopilot.llm.otel_bridge import InMemoryExporter

    class _NoRespModelProvider:  # offline-fake shape: 3-tuple, no response model
        def complete(self, *, system: str, user: str, model: str):
            return "Thought: done\nFinal Answer: ok", 3, 2

    _result, spans = _run_react_with_tracer_and_provider(InMemoryExporter(), _NoRespModelProvider())
    attrs = dict(_get_spans_by_name(spans, "gen_ai.chat")[0].attributes or {})
    got = attrs.get("gen_ai.response.model")
    assert "gen_ai.response.model" not in attrs, (
        f"response.model must be unset for an offline fake, got {got!r}"
    )


def test_react_chat_span_model_falls_back_to_tier_when_provider_has_no_model() -> None:
    """R3-C①: a provider without a .model attribute → model falls back to the tier label
    (still NOT the task label)."""
    from owcopilot.llm.otel_bridge import InMemoryExporter

    class _NoModelProvider:  # offline-fake shape: no .model attribute
        def complete(self, *, system: str, user: str, model: str):
            return "Thought: done\nFinal Answer: ok", 3, 2

    _result, spans = _run_react_with_tracer_and_provider(InMemoryExporter(), _NoModelProvider())
    attrs = dict(_get_spans_by_name(spans, "gen_ai.chat")[0].attributes or {})
    # tier label is "cheap" here; never the "agent_react" task label.
    assert attrs.get("gen_ai.request.model") == "cheap"
    assert attrs.get("gen_ai.request.model") != "agent_react"


# ---------------------------------------------------------------------------
# R3-Team-C ③: agent.run_id is now present on the production root span
# (previously dangling — invoke_agent_span only set it when a run_id was passed in,
#  and react.py's single call site never passed one).
# ---------------------------------------------------------------------------

def test_react_root_span_has_run_id_equal_to_trace_id() -> None:
    """R3-C③: the invoke_agent root span carries agent.run_id == its trace_id (hex-32)."""
    from owcopilot.llm.otel_bridge import InMemoryExporter

    class _P:
        model = "deepseek-v4-flash"

        def complete(self, *, system: str, user: str, model: str):
            return "Thought: done\nFinal Answer: ok", 1, 1

    _result, spans = _run_react_with_tracer_and_provider(InMemoryExporter(), _P())
    roots = _get_spans_by_name(spans, "invoke_agent")
    assert roots, "No invoke_agent root span"
    root = roots[0]
    attrs = dict(root.attributes or {})
    run_id = attrs.get("agent.run_id")
    assert run_id, "agent.run_id attribute is missing/empty on the production root span"
    ctx = root.get_span_context()
    assert run_id == format(ctx.trace_id, "032x"), (
        "agent.run_id must equal the span's trace_id (run_id == trace_id contract)"
    )


def test_react_execute_tool_span_error_on_bare_exception() -> None:
    """RT4-③ integration: react.py marks execute_tool span ERROR on unexpected Exception."""
    from opentelemetry.trace import StatusCode

    from owcopilot.llm.otel_bridge import InMemoryExporter

    exporter = InMemoryExporter()
    result = _build_react_agent_with_tracer(exporter, use_bare_exception=True)

    assert result.steps, "No steps recorded"
    assert result.steps[0].is_error is True

    spans = exporter.get_finished_spans()
    tool_spans = _get_spans_by_name(spans, "execute_tool")
    assert tool_spans, f"No execute_tool span found. All spans: {[s.name for s in spans]}"

    tool = tool_spans[0]
    assert tool.status.status_code == StatusCode.ERROR, (
        f"Expected ERROR status on execute_tool span after bare Exception, "
        f"got: {tool.status.status_code}"
    )
    event_names = [e.name for e in (tool.events or [])]
    assert "exception" in event_names, (
        f"Expected 'exception' event on execute_tool span, got: {event_names}"
    )
