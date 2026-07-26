"""LangChain-backed provider adapter + chat-model factory (offline, deterministic, $0).

Pins the T1a parity contract of ``llm/chat_model.py`` against the retired self-built provider:
text/usage/cached-token mapping, native tool-call mapping, the ``finish_reason == "length"``
truncation guard, openai.* error-family classification (failover auth passthrough included),
and full capability-member transparency through the resilience wrappers.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from pydantic import Field

from owcopilot.fakes import scripted_chat_model
from owcopilot.llm.chat_model import (
    FALLBACK_MODEL_ENV,
    ChatModelProvider,
    CircuitBreakerChatModel,
    build_chat_model,
    build_chat_model_with_resilience,
)
from owcopilot.llm.circuit import CIRCUIT_BREAKER_ENV, _CircuitState
from owcopilot.llm.gateway import (
    LLMGateway,
    LLMGatewayError,
    OutputTruncatedError,
    provider_supports_tools,
)
from owcopilot.llm.resilience import (
    PROVIDER_CAPABILITY_MEMBERS,
    CircuitBreakerProvider,
    CircuitOpenError,
    FailoverProvider,
)
from owcopilot.llm.router import StaticRouter
from owcopilot.llm.telemetry import PRICES, TelemetryCollector


class _RecordingFake(GenericFakeChatModel):
    """Scripted fake that records what the adapter hands to the model boundary."""

    seen_messages: list[Any] = Field(default_factory=list)
    seen_kwargs: list[dict[str, Any]] = Field(default_factory=list)
    seen_tools: list[Any] = Field(default_factory=list)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen_messages.append(list(messages))
        self.seen_kwargs.append(dict(kwargs))
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    def bind_tools(self, tools, **kwargs):
        self.seen_tools.append(list(tools))
        return self


class _RaisingFake(GenericFakeChatModel):
    """Chat model whose every call raises ``exc_factory()`` (classification / resilience tests)."""

    exc_factory: Any = None

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise self.exc_factory()

    def bind_tools(self, tools, **kwargs):
        return self


# openai's real exception family is classified purely by class NAME + message in
# ``_classify_provider_error``; these locally-defined twins carry the same names so the
# classification contract is pinned without importing the live extra.
class APITimeoutError(Exception):
    """Name twin of ``openai.APITimeoutError``."""


class RateLimitError(Exception):
    """Name twin of ``openai.RateLimitError``."""


class AuthenticationError(Exception):
    """Name twin of ``openai.AuthenticationError``."""


class APIConnectionError(Exception):
    """Name twin of ``openai.APIConnectionError``."""


def _adapter(script_or_model, **kwargs) -> ChatModelProvider:
    chat = (
        script_or_model
        if isinstance(script_or_model, GenericFakeChatModel)
        else scripted_chat_model(script_or_model)
    )
    kwargs.setdefault("model", "deepseek-v4-flash")
    return ChatModelProvider(chat_model=chat, **kwargs)


def _usage_message(
    text: str = "hello",
    *,
    finish_reason: str = "stop",
    tool_calls: list[dict[str, Any]] | None = None,
) -> AIMessage:
    return AIMessage(
        content=text,
        tool_calls=tool_calls or [],
        usage_metadata={
            "input_tokens": 1000,
            "output_tokens": 50,
            "total_tokens": 1050,
            "input_token_details": {"cache_read": 400},
        },
        response_metadata={"finish_reason": finish_reason, "model_name": "deepseek-v4-flash-0716"},
    )


# --------------------------------------------------------------------------- complete: parity
def test_complete_maps_text_usage_cached_tokens_and_response_model() -> None:
    provider = _adapter(GenericFakeChatModel(messages=iter([_usage_message()])))
    result = provider.complete(system="s", user="u", model="cheap")
    assert result == ("hello", 1000, 50, 400, "deepseek-v4-flash-0716")


def test_complete_degrades_to_zero_usage_for_fakes_without_metadata() -> None:
    # Same tolerance the gateway had for 3-tuple providers: no usage -> zeros, no crash.
    provider = _adapter(["ok"])
    assert provider.complete(system="s", user="u", model="cheap") == ("ok", 0, 0, 0, "")


def test_complete_raises_guided_truncation_error_on_length_finish() -> None:
    provider = _adapter(
        GenericFakeChatModel(messages=iter([_usage_message(finish_reason="length")])),
        max_output_tokens=50,
    )
    with pytest.raises(OutputTruncatedError, match="completion_tokens=50"):
        provider.complete(system="s", user="u", model="cheap")


def test_complete_json_mode_is_gated_on_prompt_mentioning_json() -> None:
    fake = _RecordingFake(messages=iter([AIMessage(content="a"), AIMessage(content="b")]))
    provider = _adapter(fake)  # json_mode=True default

    provider.complete(system="Return ONE JSON object.", user="make a quest", model="cheap")
    assert fake.seen_kwargs[0].get("response_format") == {"type": "json_object"}

    provider.complete(system="You are a planner.", user="steps for foo", model="cheap")
    assert "response_format" not in fake.seen_kwargs[1]

    fake_off = _RecordingFake(messages=iter([AIMessage(content="c")]))
    _adapter(fake_off, json_mode=False).complete(system="Return JSON", user="x", model="cheap")
    assert "response_format" not in fake_off.seen_kwargs[0]


def test_complete_sends_system_and_human_messages() -> None:
    fake = _RecordingFake(messages=iter([AIMessage(content="a")]))
    _adapter(fake).complete(system="sys prompt", user="user prompt", model="cheap")
    sent = fake.seen_messages[0]
    assert [type(m) for m in sent] == [SystemMessage, HumanMessage]
    assert (sent[0].content, sent[1].content) == ("sys prompt", "user prompt")


# ---------------------------------------------------------------- complete_with_tools: parity
_TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {"name": "search_lore", "parameters": {"type": "object", "properties": {}}},
    }
]
_TOOLS_MESSAGES: list[dict[str, Any]] = [
    {"role": "system", "content": "s"},
    {"role": "user", "content": "Goal: find the moon lore"},
]


def test_complete_with_tools_maps_native_tool_calls() -> None:
    provider = _adapter([("search_lore", {"query": "moon"})])
    resp = provider.complete_with_tools(
        messages=_TOOLS_MESSAGES, tools=_TOOLS_SCHEMA, model="cheap"
    )
    assert resp.wants_tool_calls
    assert len(resp.tool_calls) == 1
    call = resp.tool_calls[0]
    assert (call.call_id, call.name, call.arguments) == ("call_0", "search_lore", {"query": "moon"})


def test_complete_with_tools_final_text_answer_ends_the_loop() -> None:
    provider = _adapter(["all done"])
    resp = provider.complete_with_tools(
        messages=_TOOLS_MESSAGES, tools=_TOOLS_SCHEMA, model="cheap"
    )
    assert not resp.wants_tool_calls
    assert resp.text == "all done"


def test_complete_with_tools_converts_openai_history_and_binds_schemas() -> None:
    fake = _RecordingFake(messages=iter([AIMessage(content="done")]))
    history = _TOOLS_MESSAGES + [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_7",
                    "type": "function",
                    "function": {"name": "search_lore", "arguments": '{"query": "moon"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_7", "content": "3 hits"},
    ]
    _adapter(fake).complete_with_tools(messages=history, tools=_TOOLS_SCHEMA, model="cheap")

    assert fake.seen_tools[0] == _TOOLS_SCHEMA  # OpenAI schemas bound as-is
    sent = fake.seen_messages[0]
    assert [type(m) for m in sent] == [SystemMessage, HumanMessage, AIMessage, ToolMessage]
    assert sent[2].tool_calls[0]["name"] == "search_lore"
    assert sent[2].tool_calls[0]["args"] == {"query": "moon"}  # JSON-string args decoded
    assert sent[3].tool_call_id == "call_7"  # correlation id preserved


def test_complete_with_tools_maps_usage_and_response_model() -> None:
    msg = _usage_message(
        "",
        tool_calls=[
            {"name": "search_lore", "args": {"q": "moon"}, "id": "call_1", "type": "tool_call"}
        ],
    )
    provider = _adapter(_RecordingFake(messages=iter([msg])))
    resp = provider.complete_with_tools(
        messages=_TOOLS_MESSAGES, tools=_TOOLS_SCHEMA, model="cheap"
    )
    assert (resp.input_tokens, resp.output_tokens, resp.cached_input_tokens) == (1000, 50, 400)
    assert resp.response_model == "deepseek-v4-flash-0716"


def test_complete_with_tools_degrades_malformed_arguments_to_empty_dict() -> None:
    # Parity with the retired parser: a tool call whose argument JSON didn't parse still comes
    # back (name + id intact) with arguments={} so the registry reports the missing-arg error
    # as an observation and the loop self-corrects — never a crash, never a dropped call.
    msg = AIMessage(
        content="",
        invalid_tool_calls=[
            {
                "name": "search_lore",
                "args": '{"query": ',
                "id": "call_9",
                "error": "unterminated JSON",
                "type": "invalid_tool_call",
            }
        ],
    )
    provider = _adapter(_RecordingFake(messages=iter([msg])))
    resp = provider.complete_with_tools(
        messages=_TOOLS_MESSAGES, tools=_TOOLS_SCHEMA, model="cheap"
    )
    assert len(resp.tool_calls) == 1
    call = resp.tool_calls[0]
    assert (call.call_id, call.name, call.arguments) == ("call_9", "search_lore", {})


def test_complete_with_tools_stays_tolerant_on_length_finish() -> None:
    # The tools path deliberately does NOT raise OutputTruncatedError (same as the retired
    # provider): a truncated tool call degrades and the loop self-corrects via the observation.
    msg = _usage_message(
        "",
        finish_reason="length",
        tool_calls=[{"name": "search_lore", "args": {}, "id": "call_2", "type": "tool_call"}],
    )
    provider = _adapter(_RecordingFake(messages=iter([msg])), max_output_tokens=50)
    resp = provider.complete_with_tools(
        messages=_TOOLS_MESSAGES, tools=_TOOLS_SCHEMA, model="cheap"
    )
    assert resp.wants_tool_calls  # no raise


# ------------------------------------------------------------- error classification / failover
@pytest.mark.parametrize(
    ("exc_factory", "category"),
    [
        (lambda: APITimeoutError("Request timed out."), "timeout"),
        (lambda: RateLimitError("Error code: 429"), "rate_limit"),
        (lambda: AuthenticationError("Error code: 401 - invalid api key"), "auth"),
        (lambda: APIConnectionError("Connection error."), "connection"),
    ],
)
def test_openai_exception_family_classifies_through_the_gateway(exc_factory, category) -> None:
    # The adapter deliberately does NOT translate exceptions: ChatDeepSeek/ChatOpenAI raise the
    # openai.* family, whose class names _classify_provider_error already understands. This pins
    # that the raw exception reaches the gateway classifier unwrapped.
    provider = _adapter(_RaisingFake(messages=iter([]), exc_factory=exc_factory))
    gw = LLMGateway(providers={"cheap": provider})
    with pytest.raises(LLMGatewayError) as ei:
        gw.complete(task="plan", system="s", user="u", tier="cheap")
    assert ei.value.category == category


def test_failover_auth_error_passes_through_adapter_untouched() -> None:
    # Auth must surface as a misconfiguration, never be masked by the secondary.
    primary = _adapter(
        _RaisingFake(messages=iter([]), exc_factory=lambda: AuthenticationError("401"))
    )
    secondary = _adapter(["secondary answer"])
    fp = FailoverProvider(primary, secondary)
    with pytest.raises(AuthenticationError):
        fp.complete(system="s", user="u", model="cheap")
    assert fp.failovers == 0


def test_failover_hands_off_between_adapters_on_availability_error() -> None:
    primary = _adapter(
        _RaisingFake(messages=iter([]), exc_factory=lambda: APIConnectionError("refused"))
    )
    secondary = _adapter(["rescued"])
    fp = FailoverProvider(primary, secondary)
    text, *_ = fp.complete(system="s", user="u", model="cheap")
    assert text == "rescued"
    assert fp.failovers == 1


def test_circuit_breaker_over_adapter_opens_and_fails_fast() -> None:
    provider = _adapter(_RaisingFake(messages=iter([]), exc_factory=lambda: TimeoutError("t")))
    cb = CircuitBreakerProvider(provider, failure_threshold=1, reset_timeout_seconds=60)
    with pytest.raises(TimeoutError):
        cb.complete(system="s", user="u", model="cheap")
    with pytest.raises(CircuitOpenError):
        cb.complete(system="s", user="u", model="cheap")


def test_adapter_exposes_every_provider_capability_member() -> None:
    provider = _adapter(["x"])
    for member in PROVIDER_CAPABILITY_MEMBERS:
        assert hasattr(provider, member), f"ChatModelProvider lacks {member!r}"
    assert provider_supports_tools(provider) is True
    assert provider.model == "deepseek-v4-flash"
    # Through both resilience wrappers the capability surface stays fully visible.
    wrapped = CircuitBreakerProvider(FailoverProvider(provider, _adapter(["y"])))
    assert provider_supports_tools(wrapped) is True
    assert wrapped.model == "deepseek-v4-flash"


def test_missing_api_key_fails_fast_with_guidance(monkeypatch) -> None:
    for key in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    provider = ChatModelProvider(model="deepseek-v4-flash")  # no injected chat model
    with pytest.raises(RuntimeError, match="needs an API key"):
        provider.complete(system="s", user="u", model="cheap")


# ------------------------------------------- agent-path resilience (failover + circuit, opt-in)
def test_resilience_builder_is_a_noop_when_env_unset(monkeypatch) -> None:
    # Default (no OWCOPILOT_FALLBACK_MODEL / OWCOPILOT_CIRCUIT_BREAKER, no injected fallback): the
    # primary is returned verbatim, so the agent path is byte-for-byte identical to a run without
    # resilience — both layers are strictly opt-in.
    monkeypatch.delenv(FALLBACK_MODEL_ENV, raising=False)
    monkeypatch.delenv(CIRCUIT_BREAKER_ENV, raising=False)
    primary = scripted_chat_model(["only me"])
    assert build_chat_model_with_resilience(primary) is primary


def test_failover_hands_off_to_secondary_on_availability_error(monkeypatch) -> None:
    # An availability error (openai.APITimeoutError) is in the handled set → hand off to the
    # secondary, which answers. This pins that .with_fallbacks was given the right exception types.
    monkeypatch.delenv(CIRCUIT_BREAKER_ENV, raising=False)
    openai = pytest.importorskip("openai")
    httpx = pytest.importorskip("httpx")
    request = httpx.Request("POST", "https://api.example.com/v1/chat/completions")
    primary = _RaisingFake(
        messages=iter([]), exc_factory=lambda: openai.APITimeoutError(request=request)
    )
    secondary = scripted_chat_model(["rescued by fallback"])
    wrapped = build_chat_model_with_resilience(primary, fallback=secondary)
    result = wrapped.invoke([HumanMessage(content="hi")])
    assert result.content == "rescued by fallback"


def test_failover_passes_auth_error_through_without_handoff(monkeypatch) -> None:
    # Auth must surface as a misconfiguration, never masked by the secondary — parity with the
    # retired FailoverProvider's `auth` passthrough, here expressed by TYPE omission (auth classes
    # are deliberately NOT in exceptions_to_handle, so .with_fallbacks re-raises them).
    monkeypatch.delenv(CIRCUIT_BREAKER_ENV, raising=False)
    openai = pytest.importorskip("openai")
    httpx = pytest.importorskip("httpx")
    request = httpx.Request("POST", "https://api.example.com/v1/chat/completions")
    response = httpx.Response(401, request=request)
    primary = _RaisingFake(
        messages=iter([]),
        exc_factory=lambda: openai.AuthenticationError(message="401", response=response, body=None),
    )
    secondary = scripted_chat_model(["should not be reached"])
    wrapped = build_chat_model_with_resilience(primary, fallback=secondary)
    with pytest.raises(openai.AuthenticationError):
        wrapped.invoke([HumanMessage(content="hi")])


# ---------------------------------------- agent-path circuit breaker (CircuitBreakerChatModel)
def _raising_chat_model() -> GenericFakeChatModel:
    """A chat model whose every invoke raises — drives the circuit toward OPEN at $0."""

    class _AlwaysDown(GenericFakeChatModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            raise RuntimeError("provider down")

        def bind_tools(self, tools, **kwargs):  # noqa: ARG002
            return self

    return _AlwaysDown(messages=iter([]))


def test_circuit_breaker_closed_passes_through() -> None:
    inner = scripted_chat_model(["hello"])
    cb = CircuitBreakerChatModel(inner, _CircuitState(failure_threshold=2))
    assert cb.invoke([HumanMessage(content="hi")]).content == "hello"
    assert not cb.is_open


def test_circuit_breaker_opens_after_threshold_then_fast_fails() -> None:
    inner = _raising_chat_model()
    state = _CircuitState(failure_threshold=2, reset_timeout_seconds=30)
    cb = CircuitBreakerChatModel(inner, state)
    for _ in range(2):  # two consecutive failures open the circuit
        with pytest.raises(RuntimeError, match="provider down"):
            cb.invoke([HumanMessage(content="hi")])
    assert cb.is_open
    assert cb.trips == 1
    # OPEN: the next call fast-fails with CircuitOpenError without touching the inner model.
    with pytest.raises(CircuitOpenError):
        cb.invoke([HumanMessage(content="hi")])


def test_circuit_breaker_half_open_probe_recovers_after_cooldown() -> None:
    now = {"t": 0.0}
    calls = {"n": 0}

    class _FlakyInner:
        def invoke(self, input, config=None, **kwargs):  # noqa: A002, ARG002
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("down")
            return AIMessage(content="ok")

    state = _CircuitState(failure_threshold=1, reset_timeout_seconds=10, clock=lambda: now["t"])
    cb = CircuitBreakerChatModel(_FlakyInner(), state)
    with pytest.raises(RuntimeError):
        cb.invoke("x")  # opens (threshold=1)
    now["t"] = 5.0  # still cooling → fast fail, inner untouched
    with pytest.raises(CircuitOpenError):
        cb.invoke("x")
    assert calls["n"] == 1
    now["t"] = 11.0  # cooldown elapsed → half-open trial; inner now succeeds → circuit closes
    assert cb.invoke("x").content == "ok"
    assert not cb.is_open


def test_circuit_breaker_wraps_failover_counts_one_failure_per_attempt() -> None:
    # The breaker wraps a with_fallbacks failover (composition order: circuit OUTSIDE failover).
    # A single invoke that exhausts BOTH primary and secondary counts as ONE circuit failure.
    primary = _raising_chat_model()
    secondary = _raising_chat_model()
    failover = primary.with_fallbacks([secondary])
    cb = CircuitBreakerChatModel(failover, _CircuitState(failure_threshold=2))
    for _ in range(2):
        with pytest.raises(RuntimeError):
            cb.invoke([HumanMessage(content="hi")])
    assert cb.trips == 1  # opened once, after two whole-failover attempts (not after two providers)
    with pytest.raises(CircuitOpenError):
        cb.invoke([HumanMessage(content="hi")])


def test_circuit_breaker_bind_tools_shares_circuit_state() -> None:
    # create_agent re-binds tools every model call: the bound wrapper MUST share the circuit state,
    # else the streak would reset each call and the breaker would never open.
    inner = _raising_chat_model()
    state = _CircuitState(failure_threshold=2)
    cb = CircuitBreakerChatModel(inner, state)
    bound = cb.bind_tools([])
    assert isinstance(bound, CircuitBreakerChatModel)
    for wrapper in (cb, bound):  # one failure through each wrapper → shared streak reaches 2
        with pytest.raises(RuntimeError):
            wrapper.invoke([HumanMessage(content="hi")])
    assert cb.is_open and bound.is_open  # both see the same opened circuit
    assert state.trips == 1


def test_resilience_builder_wraps_circuit_breaker_when_env_set(monkeypatch) -> None:
    monkeypatch.delenv(FALLBACK_MODEL_ENV, raising=False)
    monkeypatch.setenv(CIRCUIT_BREAKER_ENV, "1")
    primary = scripted_chat_model(["hi"])
    wrapped = build_chat_model_with_resilience(primary)
    assert isinstance(wrapped, CircuitBreakerChatModel)


def test_resilience_builder_circuit_wraps_failover_when_both_env_set(monkeypatch) -> None:
    # Both layers on: the returned object is the circuit breaker, and its inner is the failover
    # Runnable (composition order parity with the text path's build_real_provider).
    monkeypatch.delenv(FALLBACK_MODEL_ENV, raising=False)
    monkeypatch.setenv(CIRCUIT_BREAKER_ENV, "1")
    primary = scripted_chat_model(["hi"])
    secondary = scripted_chat_model(["backup"])
    wrapped = build_chat_model_with_resilience(primary, fallback=secondary)
    assert isinstance(wrapped, CircuitBreakerChatModel)
    from langchain_core.runnables import RunnableWithFallbacks

    assert isinstance(wrapped._inner, RunnableWithFallbacks)


def test_circuit_breaker_is_accepted_by_create_agent_and_opens_on_planning_path() -> None:
    # The circuit breaker must be a drop-in agent model: create_agent duck-types on bind_tools,
    # so the breaker sits on the SAME planning path and opens after the threshold there too.
    create_agent = pytest.importorskip("langchain.agents").create_agent
    inner = _raising_chat_model()
    state = _CircuitState(failure_threshold=2, reset_timeout_seconds=30)
    cb = CircuitBreakerChatModel(inner, state)
    agent = create_agent(cb, tools=[], system_prompt="sys")
    for _ in range(2):  # inner fails → RuntimeError propagates through the graph
        with pytest.raises(RuntimeError, match="provider down"):
            agent.invoke({"messages": [HumanMessage(content="hi")]})
    with pytest.raises(CircuitOpenError):  # circuit now OPEN → fast-fail on the create_agent path
        agent.invoke({"messages": [HumanMessage(content="hi")]})


def test_circuit_breaker_does_not_double_count_telemetry_callbacks() -> None:
    # The breaker is a plain Runnable, so it does NOT start its own chat-model run: the inner model
    # fires on_chat_model_start exactly once per call (parity with .with_fallbacks — no inflation).
    from langchain_core.callbacks import BaseCallbackHandler

    class _CountStarts(BaseCallbackHandler):
        def __init__(self) -> None:
            self.starts = 0

        def on_chat_model_start(self, serialized, messages, **kwargs) -> None:  # noqa: ARG002
            self.starts += 1

    counter = _CountStarts()
    inner = scripted_chat_model(["done"])
    cb = CircuitBreakerChatModel(inner, _CircuitState(failure_threshold=5))
    cb.invoke([HumanMessage(content="hi")], config={"callbacks": [counter]})
    assert counter.starts == 1


# --------------------------------------------------------- gateway end-to-end (tuple contract)
def test_gateway_records_adapter_usage_with_cache_hit_pricing() -> None:
    telemetry = TelemetryCollector()
    provider = _adapter(GenericFakeChatModel(messages=iter([_usage_message()])))
    gw = LLMGateway(
        providers={"cheap": provider},
        router=StaticRouter(mapping={"generate": "cheap"}),
        telemetry=telemetry,
    )
    assert gw.complete(task="generate", system="s", user="u") == "hello"
    rec = telemetry.records[0]
    assert (rec.input_tokens, rec.output_tokens, rec.cached_input_tokens) == (1000, 50, 400)
    assert rec.model == "deepseek-v4-flash"  # request side: the adapter's configured id
    assert rec.response_model == "deepseek-v4-flash-0716"  # response side: what answered
    hit_p, miss_p, out_p = PRICES["cheap"]
    assert rec.cost_usd == (400 * hit_p + 600 * miss_p + 50 * out_p) / 1_000_000


# ------------------------------------------------------------------------------- the factory
def test_build_chat_model_defaults_to_deepseek() -> None:
    ChatDeepSeek = pytest.importorskip("langchain_deepseek").ChatDeepSeek
    chat = build_chat_model(model="deepseek-v4-flash", api_key="k", max_tokens=6000)
    assert isinstance(chat, ChatDeepSeek)
    assert chat.model_name == "deepseek-v4-flash"
    # Parity with the retired provider: temperature omitted -> API default, not a hardcoded 0.0.
    assert chat.temperature is None
    assert chat.max_tokens == 6000


def test_build_chat_model_routes_custom_base_url_to_openai_compat() -> None:
    ChatOpenAI = pytest.importorskip("langchain_openai").ChatOpenAI
    chat = build_chat_model(model="my-model", api_key="k", base_url="http://localhost:9/v1")
    assert isinstance(chat, ChatOpenAI)
    assert chat.openai_api_base == "http://localhost:9/v1"
    assert chat.model_name == "my-model"


def test_build_chat_model_missing_live_dependency_raises_guided_error(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "langchain_deepseek", None)  # simulate not installed
    with pytest.raises(ModuleNotFoundError, match=r"owcopilot\[live\]"):
        build_chat_model(model="deepseek-v4-flash", api_key="k")
    monkeypatch.setitem(sys.modules, "langchain_openai", None)
    with pytest.raises(ModuleNotFoundError, match=r"owcopilot\[live\]"):
        build_chat_model(model="m", api_key="k", base_url="http://localhost:9/v1")


def test_connection_probe_reports_missing_live_dependency(monkeypatch) -> None:
    # The BYO-key probe builds the adapter lazily inside its try block, so a missing live
    # extra still surfaces as the friendly missing_dependency verdict, not a raw ImportError.
    from owcopilot.app.actions.connection import probe_llm_connection_action

    monkeypatch.setitem(sys.modules, "langchain_openai", None)
    result = probe_llm_connection_action(
        base_url="https://api.example.com", api_key="sk-test", model="demo-model"
    )
    assert result["ok"] is False
    assert result["category"] == "missing_dependency"


def test_chat_model_accessor_returns_the_backing_model() -> None:
    chat = scripted_chat_model(["hi"])
    provider = _adapter(chat)
    # The public assembly point (create_agent hosts): the injected model comes back as-is.
    assert provider.chat_model() is chat
