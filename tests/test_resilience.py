"""Provider resilience: failover + circuit breaker (offline, deterministic)."""

from __future__ import annotations

import logging

import pytest

from owcopilot.llm.cache import ExactCache
from owcopilot.llm.chat_model import ChatModelProvider
from owcopilot.llm.gateway import LLMGateway, ToolCallResponse
from owcopilot.llm.resilience import (
    PROVIDER_CAPABILITY_MEMBERS,
    CircuitBreakerProvider,
    CircuitOpenError,
    FailoverProvider,
    build_real_provider,
)
from owcopilot.llm.router import StaticRouter
from owcopilot.llm.telemetry import TelemetryCollector


class _Ok:
    def __init__(self, tag: str = "ok") -> None:
        self.tag = tag
        self.calls = 0

    def complete(self, *, system: str, user: str, model: str):
        self.calls += 1
        return self.tag, 1, 1


class _Fail:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.calls = 0

    def complete(self, *, system: str, user: str, model: str):
        self.calls += 1
        raise self.exc


class _Scripted:
    """Runs the next behaviour each call: an Exception is raised, None is a success."""

    def __init__(self, script: list[Exception | None]) -> None:
        self.script = script
        self.calls = 0

    def complete(self, *, system: str, user: str, model: str):
        behaviour = self.script[self.calls]
        self.calls += 1
        if isinstance(behaviour, Exception):
            raise behaviour
        return "ok", 1, 1


class _Model:
    """Provider exposing a real ``.model`` (like ``ChatModelProvider``) and returning that id
    as its completion text, so a gateway test can see which provider actually answered."""

    def __init__(self, model: str) -> None:
        self.model = model
        self.calls = 0

    def complete(self, *, system: str, user: str, model: str):
        self.calls += 1
        return self.model, 1, 1


def _call(provider):
    return provider.complete(system="s", user="u", model="m")


# --------------------------------------------------------------------------- failover
def test_failover_falls_back_on_availability_error() -> None:
    primary = _Fail(RuntimeError("connection refused"))
    secondary = _Ok("secondary")
    fp = FailoverProvider(primary, secondary)

    text, *_ = _call(fp)
    assert text == "secondary"
    assert fp.failovers == 1
    assert secondary.calls == 1


def test_failover_does_not_mask_an_auth_error() -> None:
    # Auth fails on both providers; surface it instead of hiding behind the secondary.
    primary = _Fail(RuntimeError("401 unauthorized"))
    secondary = _Ok("secondary")
    fp = FailoverProvider(primary, secondary)

    with pytest.raises(RuntimeError, match="401"):
        _call(fp)
    assert secondary.calls == 0


def test_failover_skips_secondary_when_primary_succeeds() -> None:
    primary = _Ok("primary")
    secondary = _Ok("secondary")
    fp = FailoverProvider(primary, secondary)

    text, *_ = _call(fp)
    assert text == "primary"
    assert secondary.calls == 0
    assert fp.failovers == 0


# --------------------------------------------------------------------------- circuit breaker
def test_circuit_opens_after_threshold_and_fails_fast() -> None:
    inner = _Fail(RuntimeError("boom"))
    cb = CircuitBreakerProvider(inner, failure_threshold=2, reset_timeout_seconds=30)

    for _ in range(2):
        with pytest.raises(RuntimeError, match="boom"):
            _call(cb)
    assert inner.calls == 2
    assert cb.trips == 1
    assert cb.is_open

    # OPEN: fast-fail without touching the inner provider.
    with pytest.raises(CircuitOpenError):
        _call(cb)
    assert inner.calls == 2


def test_circuit_half_open_trial_recovers_after_cooldown() -> None:
    now = {"t": 0.0}
    inner = _Scripted([RuntimeError("boom"), RuntimeError("boom"), None])  # fail, fail, then ok
    cb = CircuitBreakerProvider(
        inner, failure_threshold=2, reset_timeout_seconds=10, clock=lambda: now["t"]
    )

    for _ in range(2):
        with pytest.raises(RuntimeError):
            _call(cb)
    assert cb.is_open

    now["t"] = 5.0  # still within cooldown -> fast fail, inner untouched
    with pytest.raises(CircuitOpenError):
        _call(cb)
    assert inner.calls == 2

    now["t"] = 11.0  # cooldown elapsed -> half-open trial; inner now succeeds -> circuit closes
    text, *_ = _call(cb)
    assert text == "ok"
    assert not cb.is_open


def test_circuit_success_resets_the_failure_streak() -> None:
    inner = _Scripted([RuntimeError("boom"), None, RuntimeError("boom")])  # fail, ok, fail
    cb = CircuitBreakerProvider(inner, failure_threshold=2)

    with pytest.raises(RuntimeError):
        _call(cb)  # streak = 1
    _call(cb)  # success resets streak to 0
    with pytest.raises(RuntimeError):
        _call(cb)  # streak = 1 again, below threshold

    assert not cb.is_open
    assert cb.trips == 0


# --------------------------------------------------------------------------- factory (env-gated)
def test_build_real_provider_is_plain_without_env(monkeypatch) -> None:
    for key in ("OWCOPILOT_FALLBACK_MODEL", "OWCOPILOT_CIRCUIT_BREAKER"):
        monkeypatch.delenv(key, raising=False)
    provider = build_real_provider("deepseek-chat")
    assert isinstance(provider, ChatModelProvider)


def test_build_real_provider_wraps_failover_then_breaker(monkeypatch) -> None:
    monkeypatch.setenv("OWCOPILOT_FALLBACK_MODEL", "gpt-4o-mini")
    monkeypatch.setenv("OWCOPILOT_CIRCUIT_BREAKER", "1")
    provider = build_real_provider("deepseek-chat")
    assert isinstance(provider, CircuitBreakerProvider)
    assert isinstance(provider.inner, FailoverProvider)
    assert isinstance(provider.inner.primary, ChatModelProvider)
    assert isinstance(provider.inner.secondary, ChatModelProvider)


# ----------------------------------------------------------- .model passthrough (gen_ai/cache key)
def test_failover_exposes_primary_model() -> None:
    fp = FailoverProvider(_Model("deepseek-v4-pro"), _Model("gpt-4o-mini"))
    # The gateway resolves the model id before the call, so it must see the primary — the
    # provider actually tried first — not the tier label.
    assert fp.model == "deepseek-v4-pro"


def test_circuit_breaker_passes_through_inner_model() -> None:
    cb = CircuitBreakerProvider(_Model("deepseek-v4-pro"))
    assert cb.model == "deepseek-v4-pro"


def test_breaker_over_failover_chains_model_to_live_primary() -> None:
    # The real build order (breaker wrapping failover wrapping the real provider): the passthrough
    # must chain all the way down to the primary's true model id.
    provider = CircuitBreakerProvider(
        FailoverProvider(_Model("deepseek-v4-pro"), _Model("gpt-4o-mini"))
    )
    assert provider.model == "deepseek-v4-pro"


def test_wrapper_model_is_empty_when_inner_has_none() -> None:
    # A bare provider with no `.model` (offline fake) must yield "" — gateway then falls back to
    # the tier label, exactly as for a plain offline provider.
    fp = FailoverProvider(_Ok("primary"), _Ok("secondary"))
    assert fp.model == ""
    assert CircuitBreakerProvider(_Ok("only")).model == ""


def _gateway_with(provider) -> tuple[LLMGateway, TelemetryCollector]:
    telemetry = TelemetryCollector()
    gateway = LLMGateway(
        providers={"cheap": provider},
        router=StaticRouter(mapping={"generate": "cheap"}),
        cache=ExactCache(),
        telemetry=telemetry,
    )
    return gateway, telemetry


def test_gateway_records_real_model_through_resilience_wrappers() -> None:
    # End-to-end: the resolved `gen_ai.request.model` (CallRecord.model, read by react.py into the
    # span) must be the real inner model, NOT the "cheap" tier label, even when failover + breaker
    # wrap the provider. This is the regression the wrapper `.model` passthrough fixes.
    provider = CircuitBreakerProvider(
        FailoverProvider(_Model("deepseek-v4-pro"), _Model("gpt-4o-mini"))
    )
    gateway, telemetry = _gateway_with(provider)

    gateway.complete(task="generate", system="s", user="u")

    assert telemetry.records[-1].model == "deepseek-v4-pro"


def test_cache_key_distinguishes_primary_and_secondary_through_wrappers() -> None:
    # The same `model` variable feeds the cache key. If it degraded to the tier label, the primary
    # and secondary (two distinct real models sharing the "cheap" tier) would collide on one key
    # and the secondary's answer could be served for the primary. With the passthrough, the primary
    # model id keys the entry; a fresh provider on a *different* model must miss, not reuse.
    primary_gateway, _ = _gateway_with(_Model("deepseek-v4-pro"))
    text_a = primary_gateway.complete(task="generate", system="s", user="u")
    assert text_a == "deepseek-v4-pro"  # filled the cache under model="deepseek-v4-pro"

    # A second gateway on a different real model, same tier/prompt: distinct model -> cache MISS.
    other = _Model("gpt-4o-mini")
    secondary_gateway = LLMGateway(
        providers={"cheap": other},
        router=StaticRouter(mapping={"generate": "cheap"}),
        cache=primary_gateway.cache,  # share the backend to prove keys differ, not the store
        telemetry=TelemetryCollector(),
    )
    text_b = secondary_gateway.complete(task="generate", system="s", user="u")
    assert text_b == "gpt-4o-mini"  # did not reuse the primary's cached answer
    assert other.calls == 1


# --------------------------------------------------- capability transparency (supports_tools etc.)
# Regression: before this, wrapping a tool-capable provider in either resilience class made
# `provider_supports_tools()` probe False (the wrapper had no `supports_tools` attribute and no
# `complete_with_tools`), so the ReAct agent's `_native_tools_available()` silently fell back to the
# degraded text loop the instant a studio opted into failover/circuit-breaking — "enabling
# resilience degrades native tool-calling to text ReAct".


class _ToolOk:
    """Fake provider that supports native tool-calling and always succeeds on both call shapes."""

    supports_tools = True
    model = "tool-primary"

    def __init__(self) -> None:
        self.calls = 0
        self.tool_calls = 0

    def complete(self, *, system: str, user: str, model: str):
        self.calls += 1
        return "text-ok", 1, 1

    def complete_with_tools(self, *, messages, tools, model: str) -> ToolCallResponse:
        self.tool_calls += 1
        return ToolCallResponse(text="tool-ok", input_tokens=1, output_tokens=1)


class _ToolFail:
    """Fake provider that declares tool support but always raises an availability error."""

    supports_tools = True
    model = "tool-primary"

    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.calls = 0

    def complete(self, *, system: str, user: str, model: str):
        self.calls += 1
        raise self.exc

    def complete_with_tools(self, *, messages, tools, model: str) -> ToolCallResponse:
        self.calls += 1
        raise self.exc


class _NoTools:
    """Fake provider exposing only the required `complete()` — no native tool-calling at all,
    exactly like the offline fakes / any provider that hasn't implemented the optional surface."""

    def __init__(self, tag: str = "secondary") -> None:
        self.tag = tag
        self.calls = 0

    def complete(self, *, system: str, user: str, model: str):
        self.calls += 1
        return self.tag, 1, 1


# ---------------------------------------------------------- protocol-completeness (drift) guard
def test_wrappers_forward_every_provider_capability_member() -> None:
    """Reflective guard over PROVIDER_CAPABILITY_MEMBERS (the single source of truth documented in
    resilience.py): both wrappers must expose every member. If a future member is added to that
    tuple (a new optional provider capability) without adding matching delegation to a wrapper,
    THIS test fails — instead of the capability silently vanishing behind resilience again."""
    fp = FailoverProvider(_ToolOk(), _ToolOk())
    cb = CircuitBreakerProvider(_ToolOk())

    for member in PROVIDER_CAPABILITY_MEMBERS:
        assert hasattr(fp, member), f"FailoverProvider does not forward {member!r}"
        assert hasattr(cb, member), f"CircuitBreakerProvider does not forward {member!r}"
        assert callable(getattr(fp, member)) or member in ("model", "supports_tools")
        assert callable(getattr(cb, member)) or member in ("model", "supports_tools")


# ---------------------------------------------------------------- supports_tools passthrough
def test_failover_supports_tools_reflects_primary_when_true() -> None:
    assert FailoverProvider(_ToolOk(), _NoTools()).supports_tools is True


def test_failover_supports_tools_reflects_primary_when_false() -> None:
    # Never borrows a tool-capable SECONDARY's capability to look better than reality — the probe
    # is prospective (primary is what a fresh call tries first), so it must stay honestly False.
    assert FailoverProvider(_NoTools(), _ToolOk()).supports_tools is False


def test_circuit_breaker_supports_tools_forwards_inner() -> None:
    assert CircuitBreakerProvider(_ToolOk()).supports_tools is True
    assert CircuitBreakerProvider(_NoTools()).supports_tools is False


def test_circuit_breaker_supports_tools_ignores_circuit_state() -> None:
    # "半开/全开状态下能力查询仍指向被包 provider 本体": capability is a static trait, unaffected by
    # circuit health — an OPEN circuit still reports the wrapped provider's true capability.
    cb = CircuitBreakerProvider(_ToolFail(RuntimeError("boom")), failure_threshold=1)
    with pytest.raises(RuntimeError):
        cb.complete(system="s", user="u", model="m")
    assert cb.is_open
    assert cb.supports_tools is True


# ------------------------------------------------- complete_with_tools failover + degradation
# The wrappers' native-tools surface is now probed directly (the gateway's complete_with_tools
# façade retired in T3); these constants feed the wrapper-level tools calls below.
_TOOLS_SCHEMA = [{"type": "function", "function": {"name": "noop", "parameters": {}}}]
_TOOLS_MESSAGES = [{"role": "user", "content": "hi"}]


def test_failover_complete_with_tools_hands_off_when_secondary_also_supports_tools() -> None:
    primary = _ToolFail(RuntimeError("connection refused"))
    secondary = _ToolOk()
    fp = FailoverProvider(primary, secondary)

    resp = fp.complete_with_tools(messages=_TOOLS_MESSAGES, tools=_TOOLS_SCHEMA, model="m")

    assert resp.text == "tool-ok"
    assert fp.failovers == 1
    assert secondary.tool_calls == 1


def test_failover_complete_with_tools_refuses_to_fake_capability_on_degraded_secondary(
    caplog,
) -> None:
    # Primary supports tools but fails; the secondary CANNOT do native tool-calling. Faking success
    # (or silently calling secondary.complete() instead) would lie about the answer's shape, so this
    # must refuse: re-raise the primary's real error and log a WARNING naming the degradation.
    primary = _ToolFail(RuntimeError("connection refused"))
    secondary = _NoTools()
    fp = FailoverProvider(primary, secondary)

    with caplog.at_level(logging.WARNING, logger="owcopilot.llm.resilience"):
        with pytest.raises(RuntimeError, match="connection refused"):
            fp.complete_with_tools(messages=_TOOLS_MESSAGES, tools=_TOOLS_SCHEMA, model="m")

    assert fp.failovers == 0  # no hand-off actually completed
    assert secondary.calls == 0  # never called — would AttributeError (no complete_with_tools)
    assert any("does not support native tool-calling" in r.message for r in caplog.records)


def test_failover_complete_with_tools_passes_through_auth_error_uncalled() -> None:
    # Same passthrough contract as complete(): an auth error must not trigger a hand-off attempt.
    primary = _ToolFail(RuntimeError("401 unauthorized"))
    secondary = _ToolOk()
    fp = FailoverProvider(primary, secondary)

    with pytest.raises(RuntimeError, match="401"):
        fp.complete_with_tools(messages=_TOOLS_MESSAGES, tools=_TOOLS_SCHEMA, model="m")
    assert secondary.tool_calls == 0
    assert fp.failovers == 0


# ------------------------------------------------- circuit breaker shares ONE circuit across shapes
def test_circuit_breaker_complete_with_tools_shares_circuit_with_complete() -> None:
    failing = _ToolFail(RuntimeError("boom"))
    cb = CircuitBreakerProvider(failing, failure_threshold=2)

    with pytest.raises(RuntimeError):
        cb.complete(system="s", user="u", model="m")  # streak = 1
    with pytest.raises(RuntimeError):
        cb.complete_with_tools(messages=_TOOLS_MESSAGES, tools=_TOOLS_SCHEMA, model="m")  # streak=2

    assert cb.is_open
    assert cb.trips == 1
    with pytest.raises(CircuitOpenError):
        cb.complete_with_tools(messages=_TOOLS_MESSAGES, tools=_TOOLS_SCHEMA, model="m")
    assert failing.calls == 2  # fast-fail never touched the inner provider a 3rd time


def test_circuit_breaker_complete_with_tools_closes_on_success() -> None:
    ok = _ToolOk()
    cb = CircuitBreakerProvider(ok, failure_threshold=1)

    resp = cb.complete_with_tools(messages=_TOOLS_MESSAGES, tools=_TOOLS_SCHEMA, model="m")

    assert resp.text == "tool-ok"
    assert not cb.is_open
