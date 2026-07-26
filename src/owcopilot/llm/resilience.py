# 供应商级韧性：主备故障转移 + 熔断器。透传除 complete() 外的可选能力成员(model/supports_tools/
# complete_with_tools),避免"开韧性即退化文本 ReAct"。
"""Provider-level resilience: failover to a secondary provider, plus a circuit breaker.

Both are ``LLMProvider`` decorators (the same ``complete()`` contract as ``ChatModelProvider``),
so they compose and the gateway is untouched. They are **opt-in via env**: ``build_real_provider``
returns a plain ``ChatModelProvider`` unless ``OWCOPILOT_FALLBACK_MODEL`` /
``OWCOPILOT_CIRCUIT_BREAKER`` are set, so default real-mode behaviour is byte-for-byte unchanged.

Why: the gateway already retries, classifies and fails *closed* (it never silently downgrades), but
a single provider has no answer to a sustained outage. Failover tries a second provider on an
availability error (never on ``auth`` — that would mask a misconfiguration). The breaker stops
hammering a hard-down provider, failing fast for a cooldown to protect latency and budget.

Capability transparency: a concrete provider's surface is bigger than the required ``complete()`` —
``model`` (cache key / ``gen_ai.request.model``) and ``supports_tools`` + ``complete_with_tools``
(native function-calling, probed by ``provider_supports_tools``) are OPTIONAL members a provider
may expose (see ``llm/gateway.py``). Wrapping a provider in either class here must not make those
members disappear: a gateway that resolves a wrapped provider and finds no ``supports_tools``
silently falls back to the text ReAct loop even though the wrapped provider natively supports
tools — the exact "enabling resilience degrades to text ReAct" regression this module now guards
against. Both wrappers forward EVERY member in :data:`PROVIDER_CAPABILITY_MEMBERS` explicitly (no
``__getattr__`` proxy / no ``wrapt`` — this project only uses that dependency-free, auditable
style); a completeness test in ``tests/test_resilience.py`` iterates the same tuple so a future
member added there and forgotten here fails loudly instead of silently degrading again.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from typing import Any

from .chat_model import ChatModelProvider
from .circuit import (
    CIRCUIT_FAILURE_THRESHOLD_ENV,
    CIRCUIT_RESET_SEC_ENV,
    CircuitOpenError,
    _CircuitState,
    circuit_enabled_from_env,
)
from .gateway import (
    LLMProvider,
    ToolCallResponse,
    _classify_provider_error,
    provider_supports_tools,
)

__all__ = [
    "CircuitBreakerProvider",
    "CircuitOpenError",
    "FailoverProvider",
    "PROVIDER_CAPABILITY_MEMBERS",
    "build_real_provider",
]

logger = logging.getLogger(__name__)

#: The provider "protocol" surface beyond the formal ``typing.Protocol`` (which only requires
#: ``complete()`` so plain text-only providers keep structurally satisfying it). These are the
#: OPTIONAL members callers duck-type-probe elsewhere in the codebase (``llm/gateway.py``'s
#: ``getattr(provider, "model", ...)`` and ``provider_supports_tools``) — the single source of
#: truth both the wrappers below and ``tests/test_resilience.py``'s completeness test read from.
PROVIDER_CAPABILITY_MEMBERS: tuple[str, ...] = (
    "complete",
    "model",
    "supports_tools",
    "complete_with_tools",
)


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

    @property
    def supports_tools(self) -> bool:
        """Native tool-calling capability probe, forwarded from PRIMARY.

        Prospective, like ``.model``: the caller (``provider_supports_tools`` via
        ``LLMGateway.complete_with_tools``) checks the capability ONCE, before any call is
        attempted — at that point whether *this specific*
        call will end up failing over is unknowable, so the probe reports the provider that will be
        tried first, exactly as ``.model`` does for the cache key. It never reports a false
        capability: if primary genuinely lacks tool support this is False, never borrowed from a
        tool-capable secondary. A failover *during* an actual ``complete_with_tools`` call to a
        secondary that lacks the capability is a separate, later concern — handled honestly (raise
        + log, never fabricate) by ``complete_with_tools`` below, not by this static probe.
        """
        return provider_supports_tools(self.primary)

    def complete_with_tools(
        self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]], model: str
    ) -> ToolCallResponse:
        """Native-tools sibling of ``complete()``: same failover contract (try primary, fail over to
        secondary on a non-passthrough availability error), plus an honest capability check the
        plain ``complete()`` path doesn't need.

        A hand-off to a secondary that cannot do native tool-calling would either raise an
        ``AttributeError`` (no ``complete_with_tools``) or silently mis-answer — neither is
        acceptable, so that case is refused explicitly: a WARNING is logged naming the capability
        degradation, and the ORIGINAL primary error is re-raised (never a fabricated
        :class:`ToolCallResponse`). ``failovers`` is only incremented on an actual hand-off, so it
        stays an honest count of calls that really completed via the secondary.
        """
        # complete_with_tools is an OPTIONAL protocol member (not on the LLMProvider Protocol, so
        # plain text-only providers keep structurally satisfying it) — mirrors the `Any` cast
        # LLMGateway.complete_with_tools uses for the same reason; the provider_supports_tools
        # guards above/below prove the member exists and is callable before each use.
        primary: Any = self.primary
        try:
            return primary.complete_with_tools(messages=messages, tools=tools, model=model)
        except Exception as exc:
            if _classify_provider_error(exc) in self.passthrough:
                raise  # e.g. auth — surface it, don't mask behind the secondary
            if not provider_supports_tools(self.secondary):
                logger.warning(
                    "FailoverProvider.complete_with_tools: primary failed with a %s error (%s) "
                    "and the failover secondary does not support native tool-calling — refusing "
                    "to fabricate a tool response; native-tools capability degrades for this call.",
                    _classify_provider_error(exc),
                    exc,
                )
                raise
            self.failovers += 1
            secondary: Any = self.secondary
            return secondary.complete_with_tools(messages=messages, tools=tools, model=model)


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
        self._state = _CircuitState(
            failure_threshold=failure_threshold,
            reset_timeout_seconds=reset_timeout_seconds,
            clock=clock,
        )

    @property
    def trips(self) -> int:
        """How many times the circuit has opened (delegated to the shared state machine)."""
        return self._state.trips

    @property
    def model(self) -> str:
        """Real model id of the wrapped provider, transparently exposed for the gateway's cache
        key and ``gen_ai.request.model``. Delegates to ``inner`` (which may itself be a
        :class:`FailoverProvider`, so the passthrough chains down to the live model). Empty if the
        inner provider exposes no ``model``."""
        return getattr(self.inner, "model", "") or ""

    @property
    def supports_tools(self) -> bool:
        """Native tool-calling capability probe, forwarded from ``inner`` REGARDLESS of circuit
        state (closed / open / half-open). Capability is a static trait of the wrapped provider,
        not a function of its current health, so an open circuit still reports ``inner``'s true
        capability honestly rather than going quiet (which would make the gateway wrongly fall back
        to the text ReAct loop for a provider that does support native tools)."""
        return provider_supports_tools(self.inner)

    @property
    def is_open(self) -> bool:
        return self._state.is_open

    def complete(self, *, system: str, user: str, model: str) -> tuple:
        return self._call_through(
            lambda: self.inner.complete(system=system, user=user, model=model)
        )

    def complete_with_tools(
        self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]], model: str
    ) -> ToolCallResponse:
        """Native-tools sibling of ``complete()``: identical circuit bookkeeping (same failure
        streak / open-state fast-fail / half-open trial — both call shapes share ONE circuit, since
        they exercise the same underlying provider connection), delegated to
        ``inner.complete_with_tools``. Assumes the caller already probed ``supports_tools`` — the
        gateway's ``LLMGateway.complete_with_tools`` does, via
        ``provider_supports_tools(provider)``, before ever calling in here — same contract
        the real provider's ``complete_with_tools`` itself relies on (no redundant self-check)."""
        # Optional protocol member — see the matching comment on FailoverProvider's version.
        inner: Any = self.inner
        return self._call_through(
            lambda: inner.complete_with_tools(messages=messages, tools=tools, model=model)
        )

    def _call_through(self, fn: Callable[[], Any]) -> Any:
        """Route both ``complete()`` and ``complete_with_tools()`` through the SAME
        :class:`~owcopilot.llm.circuit._CircuitState`, so the two call shapes share exactly one
        circuit (they exercise the same underlying provider connection) instead of drifting into
        two independently-tracked ones."""
        return self._state.run(fn)


def build_real_provider(
    model: str,
    *,
    json_mode: bool = True,
    timeout: float | None = None,
    max_output_tokens: int | None = None,
) -> LLMProvider:
    """Build the real provider — the LangChain-backed :class:`ChatModelProvider` — optionally
    wrapped with failover + a circuit breaker per env. This is the ONE birth point of every
    real provider in the product, so the T1a model-layer swap (self-built HTTP →
    ChatDeepSeek/ChatOpenAI) happened here once; the wrapping logic is untouched and purely
    additive when a studio opts in.

    Env:
      ``OWCOPILOT_FALLBACK_MODEL``            secondary model id; enables failover when set.
      ``OWCOPILOT_FALLBACK_BASE_URL`` / ``_API_KEY``  creds for the secondary provider.
      ``OWCOPILOT_CIRCUIT_BREAKER=1``         wrap in a circuit breaker.
      ``OWCOPILOT_CIRCUIT_FAILURE_THRESHOLD`` consecutive failures to open (default 5).
      ``OWCOPILOT_CIRCUIT_RESET_SEC``         open-state cooldown seconds (default 30).
    """
    provider: LLMProvider = ChatModelProvider(
        model=model, json_mode=json_mode, timeout=timeout, max_output_tokens=max_output_tokens
    )
    fallback_model = os.getenv("OWCOPILOT_FALLBACK_MODEL", "").strip()
    if fallback_model:
        secondary = ChatModelProvider(
            model=fallback_model,
            base_url_env="OWCOPILOT_FALLBACK_BASE_URL",
            api_key_env="OWCOPILOT_FALLBACK_API_KEY",
            json_mode=json_mode,
            timeout=timeout,
            max_output_tokens=max_output_tokens,
        )
        provider = FailoverProvider(provider, secondary)
    if circuit_enabled_from_env():
        provider = CircuitBreakerProvider(
            provider,
            failure_threshold=int(os.getenv(CIRCUIT_FAILURE_THRESHOLD_ENV, "5")),
            reset_timeout_seconds=float(os.getenv(CIRCUIT_RESET_SEC_ENV, "30")),
        )
    return provider
