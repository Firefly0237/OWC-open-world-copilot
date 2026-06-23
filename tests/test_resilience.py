"""Provider resilience: failover + circuit breaker (offline, deterministic)."""

from __future__ import annotations

import pytest

from owcopilot.llm.gateway import OpenAICompatProvider
from owcopilot.llm.resilience import (
    CircuitBreakerProvider,
    CircuitOpenError,
    FailoverProvider,
    build_real_provider,
)


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
    assert isinstance(provider, OpenAICompatProvider)


def test_build_real_provider_wraps_failover_then_breaker(monkeypatch) -> None:
    monkeypatch.setenv("OWCOPILOT_FALLBACK_MODEL", "gpt-4o-mini")
    monkeypatch.setenv("OWCOPILOT_CIRCUIT_BREAKER", "1")
    provider = build_real_provider("deepseek-chat")
    assert isinstance(provider, CircuitBreakerProvider)
    assert isinstance(provider.inner, FailoverProvider)
    assert isinstance(provider.inner.primary, OpenAICompatProvider)
    assert isinstance(provider.inner.secondary, OpenAICompatProvider)
