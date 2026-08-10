"""LangChain-backed provider adapter + chat-model factory (offline, deterministic, $0).

Pins the T1a parity contract of ``llm/chat_model.py`` against the retired self-built provider:
text/usage/cached-token mapping, native tool-call mapping, the ``finish_reason == "length"``
truncation guard, openai.* error-family classification (failover auth passthrough included),
and full capability-member transparency through the resilience wrappers.
"""

from __future__ import annotations

import asyncio
import os
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
    DeadlineBudgetChatModel,
    PolicyFailoverChatModel,
    build_chat_model,
    build_chat_model_with_resilience,
    resolve_chat_provider_kind,
)
from owcopilot.llm.circuit import CIRCUIT_BREAKER_ENV, _CircuitState
from owcopilot.llm.gateway import (
    LLMDeadlineExceededError,
    LLMGateway,
    LLMGatewayError,
    LLMRetryBudgetExceededError,
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


class BadRequestError(Exception):
    """Name twin of ``openai.BadRequestError``."""


class ConflictError(Exception):
    """Name twin of ``openai.ConflictError``."""


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


def test_complete_accepts_anthropic_model_metadata_key() -> None:
    message = _usage_message()
    message.response_metadata = {"stop_reason": "end_turn", "model": "claude-sonnet-5"}
    provider = _adapter(GenericFakeChatModel(messages=iter([message])), provider_kind="anthropic")

    assert provider.complete(system="s", user="u", model="cheap")[-1] == "claude-sonnet-5"


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


def test_anthropic_does_not_send_openai_response_format() -> None:
    fake = _RecordingFake(messages=iter([AIMessage(content='{"ok": true}')]))
    provider = _adapter(fake, provider_kind="anthropic")

    provider.complete(system="Return ONE JSON object.", user="make a quest", model="cheap")

    assert "response_format" not in fake.seen_kwargs[0]


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


def test_adapter_declares_sdk_retry_ownership_and_explicit_attempt_cap() -> None:
    provider = _adapter(["x"], max_retries=2)
    assert provider.owns_retry_policy is True
    assert provider.suppress_gateway_replay is True
    assert provider.handles_transport_retries is True
    assert provider.max_transport_attempts == 3


def test_missing_api_key_fails_fast_with_guidance(monkeypatch) -> None:
    for key in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    provider = ChatModelProvider(model="deepseek-v4-flash")  # no injected chat model
    with pytest.raises(RuntimeError, match="needs an API key"):
        provider.complete(system="s", user="u", model="cheap")


# ------------------------------------------- agent-path resilience (failover + circuit, opt-in)
def test_resilience_builder_always_adds_whole_chain_deadline_budget_guard(monkeypatch) -> None:
    monkeypatch.delenv(FALLBACK_MODEL_ENV, raising=False)
    monkeypatch.delenv(CIRCUIT_BREAKER_ENV, raising=False)
    primary = scripted_chat_model(["only me"])
    wrapped = build_chat_model_with_resilience(primary)
    assert isinstance(wrapped, DeadlineBudgetChatModel)
    assert wrapped.inner is primary


def test_failover_hands_off_to_secondary_on_availability_error(monkeypatch) -> None:
    # An availability error (openai.APITimeoutError) is in the handled set → hand off to the
    # secondary, which answers. This pins the runtime predicate on a real SDK exception.
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
    # provider path's shared dynamic policy.
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


@pytest.mark.parametrize("use_async", [False, True])
def test_agent_failover_real_429_negative_header_never_hands_off(
    monkeypatch, use_async: bool
) -> None:
    monkeypatch.delenv(CIRCUIT_BREAKER_ENV, raising=False)
    openai = pytest.importorskip("openai")
    httpx = pytest.importorskip("httpx")
    request = httpx.Request("POST", "https://api.example.com/v1/chat/completions")

    def rate_limit() -> Exception:
        response = httpx.Response(
            429, request=request, headers={"x-should-retry": "false"}
        )
        return openai.RateLimitError(message="429", response=response, body=None)

    primary = _RaisingFake(messages=iter([]), exc_factory=rate_limit)
    secondary = _RecordingFake(messages=iter([AIMessage(content="must not run")]))
    wrapped = build_chat_model_with_resilience(primary, fallback=secondary)

    with pytest.raises(openai.RateLimitError):
        if use_async:
            asyncio.run(wrapped.ainvoke([HumanMessage(content="hi")]))
        else:
            wrapped.invoke([HumanMessage(content="hi")])
    assert secondary.seen_messages == []


@pytest.mark.parametrize("use_async", [False, True])
def test_agent_failover_real_409_positive_header_hands_off(
    monkeypatch, use_async: bool
) -> None:
    monkeypatch.delenv(CIRCUIT_BREAKER_ENV, raising=False)
    openai = pytest.importorskip("openai")
    httpx = pytest.importorskip("httpx")
    request = httpx.Request("POST", "https://api.example.com/v1/chat/completions")

    def conflict() -> Exception:
        response = httpx.Response(
            409, request=request, headers={"x-should-retry": "true"}
        )
        return openai.ConflictError(message="409", response=response, body=None)

    primary = _RaisingFake(messages=iter([]), exc_factory=conflict)
    secondary = scripted_chat_model(["rescued"])
    wrapped = build_chat_model_with_resilience(primary, fallback=secondary)

    result = (
        asyncio.run(wrapped.ainvoke([HumanMessage(content="hi")]))
        if use_async
        else wrapped.invoke([HumanMessage(content="hi")])
    )
    assert result.content == "rescued"


# ---------------------------------------- agent-path circuit breaker (CircuitBreakerChatModel)
def _raising_chat_model() -> GenericFakeChatModel:
    """A chat model whose every invoke raises — drives the circuit toward OPEN at $0."""

    class _AlwaysDown(GenericFakeChatModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            raise TimeoutError("provider timed out")

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
        with pytest.raises(TimeoutError, match="provider timed out"):
            cb.invoke([HumanMessage(content="hi")])
    assert cb.is_open
    assert cb.trips == 1
    # OPEN: the next call fast-fails with CircuitOpenError without touching the inner model.
    with pytest.raises(CircuitOpenError):
        cb.invoke([HumanMessage(content="hi")])


def test_agent_circuit_does_not_count_bad_request() -> None:
    class _BadRequestInner:
        def invoke(self, input, config=None, **kwargs):  # noqa: A002, ARG002
            raise BadRequestError("invalid request")

    cb = CircuitBreakerChatModel(_BadRequestInner(), _CircuitState(failure_threshold=1))
    with pytest.raises(BadRequestError):
        cb.invoke("x")
    assert not cb.is_open
    assert cb.trips == 0


def test_agent_circuit_async_uses_same_health_predicate_as_sync() -> None:
    wrapped_timeout = RuntimeError("adapter failed")
    wrapped_timeout.__cause__ = TimeoutError("timed out")

    class _AsyncRaises:
        def __init__(self, exc: Exception):
            self.exc = exc

        async def ainvoke(self, input, config=None, **kwargs):  # noqa: A002, ARG002
            raise self.exc

    for exc, opens in (
        (BadRequestError("invalid request"), False),
        (RateLimitError("rate limited"), False),
        (ConflictError("request conflict"), False),
        (wrapped_timeout, True),
    ):
        cb = CircuitBreakerChatModel(_AsyncRaises(exc), _CircuitState(failure_threshold=1))
        with pytest.raises(type(exc)):
            asyncio.run(cb.ainvoke("x"))
        assert cb.is_open is opens


def test_circuit_breaker_half_open_probe_recovers_after_cooldown() -> None:
    now = {"t": 0.0}
    calls = {"n": 0}

    class _FlakyInner:
            def invoke(self, input, config=None, **kwargs):  # noqa: A002, ARG002
                calls["n"] += 1
                if calls["n"] == 1:
                    raise TimeoutError("timed out")
                return AIMessage(content="ok")

    state = _CircuitState(failure_threshold=1, reset_timeout_seconds=10, clock=lambda: now["t"])
    cb = CircuitBreakerChatModel(_FlakyInner(), state)
    with pytest.raises(TimeoutError):
        cb.invoke("x")  # opens (threshold=1)
    now["t"] = 5.0  # still cooling → fast fail, inner untouched
    with pytest.raises(CircuitOpenError):
        cb.invoke("x")
    assert calls["n"] == 1
    now["t"] = 11.0  # cooldown elapsed → half-open trial; inner now succeeds → circuit closes
    assert cb.invoke("x").content == "ok"
    assert not cb.is_open


def test_circuit_breaker_wraps_failover_counts_one_failure_per_attempt() -> None:
    # Manual generic-Runnable composition remains supported even though production uses one
    # provider-level breaker inside PolicyFailoverChatModel.
    primary = _raising_chat_model()
    secondary = _raising_chat_model()
    failover = primary.with_fallbacks([secondary])
    cb = CircuitBreakerChatModel(failover, _CircuitState(failure_threshold=2))
    for _ in range(2):
        with pytest.raises(TimeoutError):
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
        with pytest.raises(TimeoutError):
            wrapper.invoke([HumanMessage(content="hi")])
    assert cb.is_open and bound.is_open  # both see the same opened circuit
    assert state.trips == 1


def test_resilience_builder_wraps_circuit_breaker_when_env_set(monkeypatch) -> None:
    monkeypatch.delenv(FALLBACK_MODEL_ENV, raising=False)
    monkeypatch.setenv(CIRCUIT_BREAKER_ENV, "1")
    primary = scripted_chat_model(["hi"])
    wrapped = build_chat_model_with_resilience(primary)
    assert isinstance(wrapped, DeadlineBudgetChatModel)
    assert isinstance(wrapped.inner, CircuitBreakerChatModel)


def test_resilience_builder_places_breakers_inside_policy_failover(monkeypatch) -> None:
    monkeypatch.delenv(FALLBACK_MODEL_ENV, raising=False)
    monkeypatch.setenv(CIRCUIT_BREAKER_ENV, "1")
    primary = scripted_chat_model(["hi"])
    secondary = scripted_chat_model(["backup"])
    wrapped = build_chat_model_with_resilience(primary, fallback=secondary)
    assert isinstance(wrapped, PolicyFailoverChatModel)
    assert isinstance(wrapped.primary, CircuitBreakerChatModel)
    assert isinstance(wrapped.secondary, CircuitBreakerChatModel)


def test_agent_primary_breaker_opens_while_secondary_continues_serving(monkeypatch) -> None:
    monkeypatch.delenv(FALLBACK_MODEL_ENV, raising=False)
    monkeypatch.setenv(CIRCUIT_BREAKER_ENV, "1")
    monkeypatch.setenv("OWCOPILOT_CIRCUIT_FAILURE_THRESHOLD", "1")
    primary = _RaisingFake(
        messages=iter([]), exc_factory=lambda: TimeoutError("primary timed out")
    )
    secondary = _RecordingFake(
        messages=iter([AIMessage(content="backup-1"), AIMessage(content="backup-2")])
    )
    wrapped = build_chat_model_with_resilience(primary, fallback=secondary)
    assert isinstance(wrapped, PolicyFailoverChatModel)
    assert isinstance(wrapped.primary, CircuitBreakerChatModel)

    assert wrapped.invoke([HumanMessage(content="one")]).content == "backup-1"
    assert wrapped.invoke([HumanMessage(content="two")]).content == "backup-2"
    assert wrapped.primary.is_open
    assert wrapped.primary.trips == 1
    assert len(secondary.seen_messages) == 2


def test_agent_sync_guard_rejects_a_result_that_arrives_after_total_deadline() -> None:
    now = {"value": 0.0}

    class _Late:
        max_retries = 0

        def invoke(self, input, config=None, **kwargs):  # noqa: A002, ARG002
            now["value"] = 2.0
            return AIMessage(content="late")

    wrapped = DeadlineBudgetChatModel(
        _Late(), total_deadline_seconds=1, clock=lambda: now["value"]
    )
    with pytest.raises(LLMDeadlineExceededError):
        wrapped.invoke("hi")


def test_agent_async_guard_cancels_the_whole_chain_at_total_deadline() -> None:
    class _Slow:
        max_retries = 0

        async def ainvoke(self, input, config=None, **kwargs):  # noqa: A002, ARG002
            await asyncio.sleep(0.05)
            return AIMessage(content="late")

    wrapped = DeadlineBudgetChatModel(_Slow(), total_deadline_seconds=0.01)
    with pytest.raises(LLMDeadlineExceededError):
        asyncio.run(wrapped.ainvoke("hi"))


@pytest.mark.parametrize("use_async", [False, True])
def test_agent_total_transport_budget_stops_secondary(use_async: bool) -> None:
    class _Primary:
        max_retries = 0

        def invoke(self, input, config=None, **kwargs):  # noqa: A002, ARG002
            raise TimeoutError("primary timed out")

        async def ainvoke(self, input, config=None, **kwargs):  # noqa: A002, ARG002
            raise TimeoutError("primary timed out")

    class _Secondary:
        max_retries = 0

        def __init__(self) -> None:
            self.calls = 0

        def invoke(self, input, config=None, **kwargs):  # noqa: A002, ARG002
            self.calls += 1
            return AIMessage(content="secondary")

        async def ainvoke(self, input, config=None, **kwargs):  # noqa: A002, ARG002
            self.calls += 1
            return AIMessage(content="secondary")

    secondary = _Secondary()
    wrapped = PolicyFailoverChatModel(
        _Primary(), secondary, max_total_transport_attempts=1
    )

    with pytest.raises(LLMRetryBudgetExceededError):
        if use_async:
            asyncio.run(wrapped.ainvoke("hi"))
        else:
            wrapped.invoke("hi")
    assert secondary.calls == 0


def test_circuit_breaker_is_accepted_by_create_agent_and_opens_on_planning_path() -> None:
    # The circuit breaker must be a drop-in agent model: create_agent duck-types on bind_tools,
    # so the breaker sits on the SAME planning path and opens after the threshold there too.
    create_agent = pytest.importorskip("langchain.agents").create_agent
    inner = _raising_chat_model()
    state = _CircuitState(failure_threshold=2, reset_timeout_seconds=30)
    cb = CircuitBreakerChatModel(inner, state)
    agent = create_agent(cb, tools=[], system_prompt="sys")
    for _ in range(2):  # inner availability error propagates through the graph
        with pytest.raises(TimeoutError, match="provider timed out"):
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
    assert rec.gateway_invocations == 1
    assert rec.transport_attempt_cap == 3
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


def test_build_chat_model_routes_anthropic_to_native_messages_adapter() -> None:
    ChatAnthropic = pytest.importorskip("langchain_anthropic").ChatAnthropic
    chat = build_chat_model(
        model="claude-haiku-4-5-20251001",
        api_key="sk-ant-test",
        base_url="https://api.anthropic.com",
        provider_kind="anthropic",
        max_tokens=6000,
    )
    assert isinstance(chat, ChatAnthropic)
    assert chat.model == "claude-haiku-4-5-20251001"
    assert chat.max_tokens == 6000


def test_provider_kind_infers_anthropic_for_legacy_saved_url() -> None:
    assert resolve_chat_provider_kind(None, "https://api.anthropic.com/v1/") == "anthropic"
    assert resolve_chat_provider_kind(None, "http://localhost:8000/v1") == "openai_compatible"
    assert resolve_chat_provider_kind(None, None) == "deepseek"


def test_provider_wires_explicit_retry_budget_to_sdk_builder(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    fake = scripted_chat_model(["ok"])

    def _build(**kwargs):
        captured.update(kwargs)
        return fake

    monkeypatch.setenv("OWCOPILOT_OPENAI_API_KEY", "test-key")
    monkeypatch.setattr("owcopilot.llm.chat_model.build_chat_model", _build)
    provider = ChatModelProvider(model="m", max_retries=4)

    assert provider.chat_model() is fake
    assert captured["max_retries"] == 4
    assert provider.max_transport_attempts == 5


def test_build_chat_model_missing_live_dependency_raises_guided_error(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "langchain_deepseek", None)  # simulate not installed
    with pytest.raises(ModuleNotFoundError, match=r"owcopilot\[live\]"):
        build_chat_model(model="deepseek-v4-flash", api_key="k")
    monkeypatch.setitem(sys.modules, "langchain_openai", None)
    with pytest.raises(ModuleNotFoundError, match=r"owcopilot\[live\]"):
        build_chat_model(model="m", api_key="k", base_url="http://localhost:9/v1")
    monkeypatch.setitem(sys.modules, "langchain_anthropic", None)
    with pytest.raises(ModuleNotFoundError, match=r"owcopilot\[live\]"):
        build_chat_model(
            model="claude-haiku-4-5-20251001",
            api_key="k",
            provider_kind="anthropic",
        )


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


def test_connection_probe_temporarily_overrides_prefixed_credentials(monkeypatch) -> None:
    from owcopilot.app.actions.connection import probe_llm_connection_action

    captured: dict[str, str | None] = {}

    class EnvReadingProbe:
        def complete(self, **_kwargs):
            captured["base_url"] = os.getenv("OWCOPILOT_OPENAI_BASE_URL")
            captured["api_key"] = os.getenv("OWCOPILOT_OPENAI_API_KEY")
            captured["provider"] = os.getenv("OWCOPILOT_LLM_PROVIDER")
            return ("pong", 0, 0)

    monkeypatch.setenv("OWCOPILOT_OPENAI_BASE_URL", "https://stored.example")
    monkeypatch.setenv("OWCOPILOT_OPENAI_API_KEY", "stored-key")
    monkeypatch.setenv("OWCOPILOT_LLM_PROVIDER", "openai_compatible")
    result = probe_llm_connection_action(
        base_url="https://api.anthropic.com",
        api_key="probe-key",
        model="claude-haiku-4-5-20251001",
        provider_kind="anthropic",
        provider=EnvReadingProbe(),
    )

    assert result["ok"] is True
    assert captured == {
        "base_url": "https://api.anthropic.com",
        "api_key": "probe-key",
        "provider": "anthropic",
    }
    assert os.environ["OWCOPILOT_OPENAI_BASE_URL"] == "https://stored.example"
    assert os.environ["OWCOPILOT_OPENAI_API_KEY"] == "stored-key"
    assert os.environ["OWCOPILOT_LLM_PROVIDER"] == "openai_compatible"


def test_chat_model_accessor_returns_the_backing_model() -> None:
    chat = scripted_chat_model(["hi"])
    provider = _adapter(chat)
    # The public assembly point (create_agent hosts): the injected model comes back as-is.
    assert provider.chat_model() is chat
