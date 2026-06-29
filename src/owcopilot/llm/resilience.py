"""Provider-level resilience: failover to a secondary provider, plus a circuit breaker.

Both are ``LLMProvider`` decorators (the same ``complete()`` contract as ``OpenAICompatProvider``),
so they compose and the gateway is untouched. They are **opt-in via env**: ``build_real_provider``
returns a plain ``OpenAICompatProvider`` unless ``OWCOPILOT_FALLBACK_MODEL`` /
``OWCOPILOT_CIRCUIT_BREAKER`` are set, so default real-mode behaviour is byte-for-byte unchanged.

Why: the gateway already retries, classifies and fails *closed* (it never silently downgrades), but
a single provider has no answer to a sustained outage. Failover tries a second provider on an
availability error (never on ``auth`` — that would mask a misconfiguration). The breaker stops
hammering a hard-down provider, failing fast for a cooldown to protect latency and budget.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable

from .gateway import LLMProvider, OpenAICompatProvider, _classify_provider_error


class CircuitOpenError(RuntimeError):
    """Raised by :class:`CircuitBreakerProvider` when the circuit is open — fail fast, no call."""


class FailoverProvider:
    """Try ``primary``; on an availability error, fall back to ``secondary``.

    Errors in ``passthrough`` (default: ``auth``) are re-raised, never failed over: they would fail
    on both providers and must surface as a misconfiguration. ``failovers`` counts hand-offs.
    """

    def __init__(
        self,
        primary: LLMProvider,
        secondary: LLMProvider,
        *,
        passthrough: frozenset[str] = frozenset({"auth"}),
    ) -> None:
        self.primary = primary
        self.secondary = secondary
        self.passthrough = passthrough
        self.failovers = 0

    @property
    def model(self) -> str:
        """Real model id of the provider a call will hit first, for the gateway's cache key and
        ``gen_ai.request.model``.

        The gateway resolves this *before* the call, so it reflects the primary — the provider
        actually tried first. Without it the gateway's ``getattr(provider, "model", None)`` would
        miss (a wrapper has no own ``model``) and fall back to the tier label, collapsing the
        primary's and secondary's distinct models onto one tier in both the cache key and OTEL —
        the very ``gen_ai.request.model`` degradation the model-back-fill exists to prevent. Empty
        if the inner provider exposes no ``model`` (e.g. an offline fake).
        """
        return getattr(self.primary, "model", "") or ""

    def complete(self, *, system: str, user: str, model: str) -> tuple:
        try:
            return self.primary.complete(system=system, user=user, model=model)
        except Exception as exc:
            if _classify_provider_error(exc) in self.passthrough:
                raise  # e.g. auth — surface it, don't mask behind the secondary
            self.failovers += 1
            return self.secondary.complete(system=system, user=user, model=model)


class CircuitBreakerProvider:
    """Wrap a provider with a fixed-threshold circuit breaker.

    CLOSED: calls pass through; ``failure_threshold`` consecutive failures open the circuit. OPEN:
    calls fail fast with :class:`CircuitOpenError` for ``reset_timeout_seconds``; the first call
    after the cooldown is a half-open trial — success closes it, failure re-opens it. In-memory /
    single-process (like the rate limiter); a multi-instance deploy would share the state. ``trips``
    counts how many times the circuit has opened.
    """

    def __init__(
        self,
        inner: LLMProvider,
        *,
        failure_threshold: int = 5,
        reset_timeout_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.inner = inner
        self.failure_threshold = max(1, failure_threshold)
        self.reset_timeout_seconds = max(0.0, reset_timeout_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        self._failures = 0
        self._opened_at: float | None = None  # None = closed
        self.trips = 0

    @property
    def model(self) -> str:
        """Real model id of the wrapped provider, transparently exposed for the gateway's cache
        key and ``gen_ai.request.model``. Delegates to ``inner`` (which may itself be a
        :class:`FailoverProvider`, so the passthrough chains down to the live model). Empty if the
        inner provider exposes no ``model``."""
        return getattr(self.inner, "model", "") or ""

    @property
    def is_open(self) -> bool:
        with self._lock:
            return self._opened_at is not None and (
                self._clock() - self._opened_at < self.reset_timeout_seconds
            )

    def complete(self, *, system: str, user: str, model: str) -> tuple:
        with self._lock:
            if self._opened_at is not None:
                if self._clock() - self._opened_at < self.reset_timeout_seconds:
                    raise CircuitOpenError("provider circuit is open; failing fast")
                # cooldown elapsed -> fall through as a half-open trial
        try:
            result = self.inner.complete(system=system, user=user, model=model)
        except Exception:
            with self._lock:
                self._failures += 1
                if self._failures >= self.failure_threshold:
                    if self._opened_at is None:
                        self.trips += 1
                    self._opened_at = self._clock()
            raise
        with self._lock:  # a success closes the circuit
            self._failures = 0
            self._opened_at = None
        return result


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def build_real_provider(
    model: str,
    *,
    json_mode: bool = True,
    timeout: float | None = None,
    max_output_tokens: int | None = None,
) -> LLMProvider:
    """Build the real OpenAI-compatible provider, optionally wrapped with failover + a circuit
    breaker per env. With no env set this is exactly ``OpenAICompatProvider(...)`` — so existing
    real-mode behaviour is unchanged; the resilience is purely additive when a studio opts in.

    Env:
      ``OWCOPILOT_FALLBACK_MODEL``            secondary model id; enables failover when set.
      ``OWCOPILOT_FALLBACK_BASE_URL`` / ``_API_KEY``  creds for the secondary provider.
      ``OWCOPILOT_CIRCUIT_BREAKER=1``         wrap in a circuit breaker.
      ``OWCOPILOT_CIRCUIT_FAILURE_THRESHOLD`` consecutive failures to open (default 5).
      ``OWCOPILOT_CIRCUIT_RESET_SEC``         open-state cooldown seconds (default 30).
    """
    provider: LLMProvider = OpenAICompatProvider(
        model=model, json_mode=json_mode, timeout=timeout, max_output_tokens=max_output_tokens
    )
    fallback_model = os.getenv("OWCOPILOT_FALLBACK_MODEL", "").strip()
    if fallback_model:
        secondary = OpenAICompatProvider(
            model=fallback_model,
            base_url_env="OWCOPILOT_FALLBACK_BASE_URL",
            api_key_env="OWCOPILOT_FALLBACK_API_KEY",
            json_mode=json_mode,
            timeout=timeout,
            max_output_tokens=max_output_tokens,
        )
        provider = FailoverProvider(provider, secondary)
    if _env_flag("OWCOPILOT_CIRCUIT_BREAKER"):
        provider = CircuitBreakerProvider(
            provider,
            failure_threshold=int(os.getenv("OWCOPILOT_CIRCUIT_FAILURE_THRESHOLD", "5")),
            reset_timeout_seconds=float(os.getenv("OWCOPILOT_CIRCUIT_RESET_SEC", "30")),
        )
    return provider
