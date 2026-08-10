# 全系统每次模型调用的唯一咽喉点。
# 关键：缓存/路由/重试/成本遥测/OTEL/原生 tools 都收束在这一层——换供应商、限成本、加观测只改这里。
"""THE single chokepoint for every model call in the system.

Cache, router and telemetry all hang off this one method, and call sites never touch a provider
directly — so caching, routing and cost controls are added here once and nothing else changes.
"""

from __future__ import annotations

import os
import random
import re
import time
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC
from email.utils import parsedate_to_datetime
from typing import Any, Protocol

from ..errors import OWCopilotError
from ..fakes import MockProvider as MockProvider
from .cache import CacheBackend, CacheKey, NoOpCache
from .router import Router, StaticRouter
from .telemetry import CallRecord, TelemetryCollector

# Offline/fake LLM providers are a TEST, CI and eval fixture — never a shipped product mode. A real
# deployment must connect a model for any AI feature (generate / ask / extract …), so it can never
# silently serve canned output as if it were the model's. The fixture is enabled only by an explicit
# opt-in the test conftest sets; the eval harness builds its gateways straight from the doubles and
# so bypasses this gate entirely (those gateways are constructed directly, not via the runtime
# builders that call `require_offline_llm_allowed`).
OFFLINE_LLM_ENV = "OWCOPILOT_ALLOW_OFFLINE_LLM"
OFFLINE_LLM_FORBIDDEN_MESSAGE = (
    "未接入模型：AI 功能（创世 / 生成 / 问答 / 提炼）需要先在「设置」接入服务商与 API Key。"
    "（离线占位模型仅用于测试与 CI，不作为产品能力提供。）"
)


def offline_llm_allowed() -> bool:
    """True only when the offline fake-LLM fixture is explicitly enabled (tests / CI / eval)."""
    return os.getenv(OFFLINE_LLM_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


# Item 6: lesson-injection kill switch.  Set OWCOPILOT_INJECT_LESSONS=0 (or "false"/"no"/"off")
# to disable lesson injection globally — e.g. when the lesson archive is suspected of containing
# bad data and you need to stop it affecting generations without restarting or modifying the DB.
# Default is "1" (enabled) so existing behaviour is unchanged.
LESSON_INJECTION_ENV = "OWCOPILOT_INJECT_LESSONS"


def lesson_injection_enabled() -> bool:
    """Return False only when OWCOPILOT_INJECT_LESSONS is explicitly set to a falsy value."""
    return os.getenv(LESSON_INJECTION_ENV, "1").strip().lower() not in {"0", "false", "no", "off"}


def require_offline_llm_allowed() -> None:
    """Fail closed before a runtime path would hand back fake LLM output to a real caller."""
    if not offline_llm_allowed():
        raise RuntimeError(OFFLINE_LLM_FORBIDDEN_MESSAGE)


class LLMProvider(Protocol):
    def complete(self, *, system: str, user: str, model: str) -> tuple[Any, ...]:
        """Return (text, input_tokens, output_tokens[, cached_input_tokens[, response_model]]).

        Elements 4 and 5 are optional and read positionally by the gateway:
          * element 4 (``cached_input_tokens``): a provider that knows its server-side
            prefix-cache usage (e.g. DeepSeek's ``prompt_cache_hit_tokens``) returns it so the
            gateway can price those tokens at the cheaper cache-hit rate. Providers that don't
            (the offline fakes) omit it and the gateway treats cached tokens as 0.
          * element 5 (``response_model``): the model id reported by the *API response body*
            (``resp.model``) — the model that actually answered. This differs from the
            request-side ``model`` arg (the tier label) and from the provider's configured
            ``.model`` (what we asked for). It exists so OTEL can set ``gen_ai.response.model``
            honestly, and — critically — so a failover hand-off naturally carries the
            *secondary's* model out (the inner provider that answered fills it). Offline fakes
            omit it; the gateway then leaves ``gen_ai.response.model`` unset rather than guessing.
        ``model`` (the argument) is the tier label.
        """
        ...

    # OPTIONAL native tool-calling surface (probed via ``provider_supports_tools``):
    #   supports_tools: bool                      — truthy iff the provider implements the below.
    #   complete_with_tools(*, messages, tools, model) -> ToolCallResponse
    # Providers that don't implement these (the offline fakes) simply omit them; the agent then
    # falls back to the text ReAct path. They are intentionally NOT declared as required Protocol
    # members so existing text-only providers still structurally satisfy ``LLMProvider``.
    #
    # OPTIONAL retry-policy surface:
    #   owns_retry_policy: bool                  — SDK/composite owns its replay orchestration.
    #   suppress_gateway_replay: bool            — Gateway must invoke this provider exactly once.
    #   max_transport_attempts: int              — hard ceiling inside one provider call.
    #   handles_transport_retries: bool          — compatibility alias for older integrations.
    # The suppression flag prevents multiplying SDK retry budgets or replaying a complete
    # primary->secondary failover chain from the outside.


# ---------------------------------------------------------------------------
# Native (function-calling) tool support — opt-in; the text ReAct path is unchanged.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolCall:
    """One tool the model asked to call, parsed from an OpenAI ``tool_calls`` entry.

    ``call_id`` is the provider-assigned id we must echo back on the tool-result message so the
    model can correlate the result with its request. ``arguments`` is the already-JSON-decoded
    argument dict (the API delivers it as a JSON string; the provider decodes it).
    """

    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ToolCallResponse:
    """The structured result of one ``complete_with_tools`` turn.

    Exactly one of the two states is meaningful:
      * ``tool_calls`` non-empty → the model wants to call tools (the agent executes them and
        feeds results back for the next turn).
      * ``tool_calls`` empty → the model produced a final ``text`` answer and the loop ends.
    Token / response-model fields mirror the plain ``complete`` tuple so telemetry stays honest.
    """

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    response_model: str = ""

    @property
    def wants_tool_calls(self) -> bool:
        return bool(self.tool_calls)


def provider_supports_tools(provider: Any) -> bool:
    """Capability probe: True only when *provider* declares native tool-calling support.

    A provider opts in by exposing a truthy ``supports_tools`` attribute AND a callable
    ``complete_with_tools``. Everything else (the offline fakes, any provider that hasn't
    implemented the contract) probes False, so the agent transparently falls back to the text
    ReAct path. The probe never raises — an unexpected provider shape simply means "no native
    tools".
    """
    try:
        return bool(getattr(provider, "supports_tools", False)) and callable(
            getattr(provider, "complete_with_tools", None)
        )
    except Exception:
        return False


def provider_suppresses_gateway_replay(provider: Any) -> bool:
    """Whether one call already owns retry/failover policy and must not be replayed by Gateway.

    New providers declare ``suppress_gateway_replay`` / ``owns_retry_policy``. The old
    ``handles_transport_retries`` marker remains a compatibility fallback for integrations built
    against the first version of this contract.
    """
    for member in (
        "suppress_gateway_replay",
        "owns_retry_policy",
        "handles_transport_retries",
    ):
        try:
            if bool(getattr(provider, member, False)):
                return True
        except Exception:
            continue
    return False


def provider_handles_transport_retries(provider: Any) -> bool:
    """Compatibility alias for :func:`provider_suppresses_gateway_replay`."""
    return provider_suppresses_gateway_replay(provider)


def provider_transport_attempt_cap(provider: Any) -> int:
    """Declared transport-attempt ceiling inside one provider invocation; one if unknown."""
    try:
        declared = getattr(provider, "max_transport_attempts", None)
        if declared is None:
            sdk_retries = getattr(provider, "max_retries", None)
            declared = int(sdk_retries) + 1 if sdk_retries is not None else 1
        return max(1, int(declared))
    except Exception:
        return 1


def provider_manages_invocation_budget(provider: Any) -> bool:
    """Whether a composite reserves the budget of its concrete children itself.

    A failover must reserve primary and secondary separately so an unused secondary is not charged;
    a circuit breaker must reserve only *after* its guard admits the call. Bare SDK and legacy
    providers return ``False`` and are reserved by their immediate caller.
    """
    try:
        return bool(getattr(provider, "manages_invocation_budget", False))
    except Exception:
        return False


class LLMDeadlineExceededError(OWCopilotError, TimeoutError):
    """The end-to-end model-call deadline expired before a safe next operation."""

    category = "deadline"


class LLMRetryBudgetExceededError(OWCopilotError, RuntimeError):
    """The next provider invocation could exceed the request's transport-attempt ceiling."""

    category = "retry_budget"


@dataclass(slots=True)
class _InvocationBudget:
    """Context-local deadline and conservative transport-attempt accounting for one request."""

    deadline: float | None
    remaining_transport_attempts: int
    clock: Callable[[], float]
    consumed_transport_attempt_cap: int = 0

    def ensure_time(self) -> None:
        if self.deadline is not None and self.clock() >= self.deadline:
            raise LLMDeadlineExceededError("LLM request total deadline exceeded")

    def reserve(self, attempts: int) -> None:
        self.ensure_time()
        attempts = max(1, attempts)
        if attempts > self.remaining_transport_attempts:
            raise LLMRetryBudgetExceededError(
                "LLM request transport retry budget exhausted before provider invocation"
            )
        self.remaining_transport_attempts -= attempts
        self.consumed_transport_attempt_cap += attempts


_INVOCATION_BUDGET: ContextVar[_InvocationBudget | None] = ContextVar(
    "owcopilot_llm_invocation_budget", default=None
)


def prepare_provider_invocation(provider: Any, *, attempt_cap: int | None = None) -> None:
    """Admit one concrete provider call under the active request budget, if any.

    Provider composites call this immediately before invoking each child. Direct provider use has
    no active Gateway scope and therefore remains unchanged.
    """
    budget = _INVOCATION_BUDGET.get()
    if budget is None:
        return
    budget.ensure_time()
    if not provider_manages_invocation_budget(provider):
        budget.reserve(
            provider_transport_attempt_cap(provider) if attempt_cap is None else attempt_cap
        )


@contextmanager
def invocation_budget_scope(
    *,
    total_deadline_seconds: float | None,
    max_transport_attempts: int,
    clock: Callable[[], float] = time.monotonic,
) -> Any:
    """Create one context-local whole-chain budget, preserving an existing outer scope."""
    existing = _INVOCATION_BUDGET.get()
    if existing is not None:
        yield existing
        return
    deadline = (
        clock() + total_deadline_seconds
        if total_deadline_seconds is not None and total_deadline_seconds > 0
        else None
    )
    budget = _InvocationBudget(
        deadline=deadline,
        remaining_transport_attempts=max(0, max_transport_attempts),
        clock=clock,
    )
    token = _INVOCATION_BUDGET.set(budget)
    try:
        yield budget
    finally:
        _INVOCATION_BUDGET.reset(token)


def ensure_invocation_time() -> None:
    """Raise when the current whole-chain deadline has expired; no-op outside a scope."""
    budget = _INVOCATION_BUDGET.get()
    if budget is not None:
        budget.ensure_time()


def remaining_invocation_time() -> float | None:
    """Seconds left in the current whole-chain deadline, or ``None`` when unbounded/unscoped."""
    budget = _INVOCATION_BUDGET.get()
    if budget is None or budget.deadline is None:
        return None
    return max(0.0, budget.deadline - budget.clock())


class OutputTruncatedError(OWCopilotError, RuntimeError):
    """The model's reply was cut off by the output-token cap (``finish_reason == "length"``).

    Raised by the *text* ``complete`` path only: there a truncated reply is guaranteed garbage
    (half a JSON object), so failing loud with guidance beats letting a downstream parser report
    an unhelpful "no balanced JSON span". The tools path deliberately stays tolerant — a truncated
    tool call degrades to ``{}`` and the loop self-corrects via the error observation.
    """

    def __init__(self, *, completion_tokens: int, cap: int):
        self.completion_tokens = completion_tokens
        self.cap = cap
        super().__init__(
            f"model output was cut off by the output-token cap "
            f"(finish_reason='length', completion_tokens={completion_tokens}, cap={cap}). "
            f"Reasoning models (e.g. deepseek-v4-flash) spend part of the cap on hidden "
            f"reasoning before the visible answer. Raise OWCOPILOT_MAX_OUTPUT_TOKENS (or pass "
            f"max_output_tokens=) to give the reply room, or simplify the request."
        )


class LLMGatewayError(OWCopilotError, RuntimeError):
    """Provider failure after gateway retries."""

    def __init__(
        self,
        *,
        task: str,
        tier: str,
        category: str,
        cause: Exception,
        gateway_invocations: int | None = None,
        transport_attempt_cap: int = 0,
        attempts: int | None = None,
    ):
        # ``attempts`` is retained as an input/output alias for callers built against the old API;
        # it always means Gateway-level provider invocations, never inferred SDK HTTP attempts.
        if gateway_invocations is None:
            gateway_invocations = attempts if attempts is not None else 0
        self.task = task
        self.tier = tier
        self.category = category
        self.gateway_invocations = max(0, gateway_invocations)
        self.transport_attempt_cap = max(0, transport_attempt_cap)
        self.attempts = self.gateway_invocations
        self.cause = cause
        super().__init__(
            f"LLM provider call failed: task={task!r}, tier={tier!r}, category={category}, "
            f"gateway_invocations={self.gateway_invocations}, "
            f"transport_attempt_cap={self.transport_attempt_cap}, cause={cause}"
        )


@dataclass(frozen=True)
class _GatewayCallOutcome:
    result: tuple[Any, ...]
    gateway_invocations: int
    transport_attempt_cap: int


class LLMGateway:
    def __init__(
        self,
        providers: dict[str, LLMProvider],
        *,
        router: Router | None = None,
        cache: CacheBackend | None = None,
        telemetry: TelemetryCollector | None = None,
        max_retries: int = 0,
        retry_backoff_seconds: float = 0.0,
        retry_backoff_cap_seconds: float = 30.0,
        total_deadline_seconds: float | None = None,
        max_total_transport_attempts: int | None = None,
        namespace: str = "",
        _clock: Callable[[], float] = time.monotonic,
        _sleep: Callable[[float], None] = time.sleep,
        _uniform: Callable[[float, float], float] = random.uniform,
        _wall_clock: Callable[[], float] = time.time,
    ):
        self.providers = providers
        self.router = router or StaticRouter()
        self.cache = cache or NoOpCache()
        self.telemetry = telemetry or TelemetryCollector()
        self.max_retries = max(0, max_retries)
        self.retry_backoff_seconds = max(0.0, retry_backoff_seconds)
        self.retry_backoff_cap_seconds = max(0.0, retry_backoff_cap_seconds)
        if total_deadline_seconds is None:
            total_deadline_seconds = float(
                os.getenv("OWCOPILOT_LLM_TOTAL_DEADLINE_SEC", "120")
            )
        self.total_deadline_seconds = (
            total_deadline_seconds if total_deadline_seconds > 0 else None
        )
        if max_total_transport_attempts is None:
            configured_attempts = os.getenv("OWCOPILOT_LLM_MAX_TRANSPORT_ATTEMPTS", "").strip()
            max_total_transport_attempts = (
                max(0, int(configured_attempts)) if configured_attempts else None
            )
        self.max_total_transport_attempts = max_total_transport_attempts
        self._clock = _clock
        self._sleep = _sleep
        self._uniform = _uniform
        self._wall_clock = _wall_clock
        # Scopes every cache key this gateway builds to one project, so a shared process/app
        # lifetime cache can't serve project B the completion generated for project A. "" = global.
        self.namespace = namespace

    def _resolve_provider(self, *, task: str, tier: str | None) -> tuple[str, LLMProvider]:
        """Route *task* to a tier and return ``(tier, provider)``, with a guided error when the
        chosen tier has no registered provider (memory red line: "guided errors, not raw")."""
        tier = self.router.choose(task=task, hint=tier)
        try:
            return tier, self.providers[tier]
        except KeyError:
            available = ", ".join(sorted(self.providers)) or "(none)"
            raise LLMGatewayError(
                task=task,
                tier=tier,
                category="config",
                gateway_invocations=0,
                transport_attempt_cap=0,
                cause=KeyError(
                    f"router chose tier {tier!r} for task {task!r}, but no provider is registered "
                    f"for it. Registered tiers: {available}. Register a provider for {tier!r} or "
                    f"adjust the router mapping so {task!r} routes to a registered tier."
                ),
            ) from None

    def complete(self, *, task: str, system: str, user: str, tier: str | None = None) -> str:
        tier, provider = self._resolve_provider(task=task, tier=tier)
        # The cache key must include the *real* model behind the tier: `llm_model` is request-driven
        # while the tier label stays "cheap", so two different models share a tier — without the
        # model id a request that switches models would be served the other model's cached answer.
        model = getattr(provider, "model", None) or tier
        key = CacheKey(tier=tier, system=system, user=user, namespace=self.namespace, model=model)

        t0 = time.perf_counter()
        cached = self.cache.get(key)
        if cached is not None:  # CLIENT-side hit: no provider call, $0
            self.telemetry.record(
                CallRecord(
                    task=task,
                    tier=tier,
                    input_tokens=0,
                    output_tokens=0,
                    cache_hit=True,
                    latency_ms=(time.perf_counter() - t0) * 1000,
                    model=model,
                )
            )
            return cached

        try:
            outcome = self._complete_with_retries(
                provider, task=task, tier=tier, system=system, user=user
            )
        except LLMGatewayError as exc:
            # Failure telemetry stores only stable classification/counters. Provider messages can
            # echo request bodies or credentials, so neither ``cause`` nor prompt text is recorded.
            self.telemetry.record(
                CallRecord(
                    task=task,
                    tier=tier,
                    input_tokens=0,
                    output_tokens=0,
                    latency_ms=(time.perf_counter() - t0) * 1000,
                    model=model,
                    gateway_invocations=exc.gateway_invocations,
                    transport_attempt_cap=exc.transport_attempt_cap,
                    success=False,
                    error_category=exc.category,
                )
            )
            raise
        result = outcome.result
        # Providers return (text, in, out[, cached_in[, response_model]]); element 4 is the
        # provider's server-side prefix-cache hit tokens (priced at the cheap cache-hit rate), and
        # element 5 is the model id from the API response body (the model that actually answered).
        text, in_tok, out_tok = str(result[0]), result[1], result[2]
        cached_in = result[3] if len(result) > 3 else 0
        # response_model is whatever the *answering* provider reported — for a failover this is the
        # secondary's id, since the inner provider that handled the call fills it. "" when the
        # provider can't report it (offline fakes): the OTEL response.model attribute is then left
        # unset rather than back-filled with the request-side model (would be a lie on failover).
        response_model = str(result[4]) if len(result) > 4 else ""
        self.telemetry.record(
            CallRecord(
                task=task,
                tier=tier,
                input_tokens=in_tok,
                output_tokens=out_tok,
                cached_input_tokens=cached_in,
                latency_ms=(time.perf_counter() - t0) * 1000,
                model=model,
                response_model=response_model,
                gateway_invocations=outcome.gateway_invocations,
                transport_attempt_cap=outcome.transport_attempt_cap,
            )
        )
        if text:  # never memoize an empty/failed completion
            self.cache.set(key, text)
        return text

    def _complete_with_retries(
        self, provider: LLMProvider, *, task: str, tier: str, system: str, user: str
    ) -> _GatewayCallOutcome:
        # An SDK-backed provider owns transport retries; a failover composite owns the WHOLE
        # primary/secondary orchestration even when both inners are legacy single-shot providers.
        # In either case Gateway invokes it once. Only a bare legacy provider uses this outer loop.
        suppress_replay = provider_suppresses_gateway_replay(provider)
        attempts = 1 if suppress_replay else self.max_retries + 1
        per_invocation_cap = provider_transport_attempt_cap(provider)
        derived_transport_budget = attempts * per_invocation_cap
        transport_budget = derived_transport_budget
        if self.max_total_transport_attempts is not None:
            transport_budget = min(transport_budget, self.max_total_transport_attempts)
        deadline = (
            self._clock() + self.total_deadline_seconds
            if self.total_deadline_seconds is not None
            else None
        )
        budget = _InvocationBudget(
            deadline=deadline,
            remaining_transport_attempts=transport_budget,
            clock=self._clock,
        )
        budget_token = _INVOCATION_BUDGET.set(budget)
        last_exc: Exception | None = None
        made = 0
        try:
            for attempt in range(1, attempts + 1):
                try:
                    prepare_provider_invocation(provider)
                except (LLMDeadlineExceededError, LLMRetryBudgetExceededError) as exc:
                    last_exc = exc
                    break
                made += 1
                try:
                    result = provider.complete(system=system, user=user, model=tier)
                    # A leaf call may run past the deadline; sync Python cannot safely cancel it,
                    # but a late result must not silently pass an end-to-end deadline contract.
                    budget.ensure_time()
                    return _GatewayCallOutcome(
                        result=result,
                        gateway_invocations=made,
                        transport_attempt_cap=budget.consumed_transport_attempt_cap,
                    )
                except Exception as exc:
                    last_exc = exc
                    try:
                        budget.ensure_time()
                    except LLMDeadlineExceededError as deadline_exc:
                        last_exc = deadline_exc
                        break
                    # Retry only failures that indicate provider availability. Authentication,
                    # permission, bad-request, output-budget and unknown application errors cannot
                    # be repaired by replaying the same request and therefore fail immediately.
                    if not should_gateway_retry(exc) or attempt >= attempts:
                        break

                    retry_after = _retry_after_seconds(exc, wall_clock=self._wall_clock)
                    if retry_after is None:
                        exponential_cap = min(
                            self.retry_backoff_cap_seconds,
                            self.retry_backoff_seconds * (2 ** (attempt - 1)),
                        )
                        delay = self._uniform(0.0, exponential_cap) if exponential_cap else 0.0
                    else:
                        delay = retry_after
                    if delay > 0:
                        if deadline is not None and self._clock() + delay >= deadline:
                            last_exc = LLMDeadlineExceededError(
                                "Retry-After/backoff would exceed LLM request total deadline"
                            )
                            break
                        self._sleep(delay)
        finally:
            _INVOCATION_BUDGET.reset(budget_token)
        assert last_exc is not None
        raise LLMGatewayError(
            task=task,
            tier=tier,
            category=_classify_provider_error(last_exc),
            gateway_invocations=made,
            transport_attempt_cap=budget.consumed_transport_attempt_cap,
            cause=last_exc,
        ) from last_exc


@dataclass(frozen=True)
class ProviderErrorInfo:
    """Stable classification plus an optional provider-supplied replay directive."""

    category: str
    retry_hint: bool | None = None
    status_code: int | None = None


_CATEGORY_ALIASES = {
    "auth": "auth",
    "authentication": "auth",
    "permission": "permission",
    "permission_denied": "permission",
    "bad_request": "bad_request",
    "invalid_request": "bad_request",
    "config": "config",
    "output_budget": "output_budget",
    "output_truncated": "output_budget",
    "timeout": "timeout",
    "rate_limit": "rate_limit",
    "conflict": "conflict",
    "circuit_open": "circuit_open",
    "deadline": "deadline",
    "retry_budget": "retry_budget",
    "connection": "connection",
    "server_error": "server_error",
    "provider_error": "provider_error",
}

_EXACT_EXCEPTION_CATEGORIES = {
    "AuthenticationError": "auth",
    "PermissionDeniedError": "permission",
    "BadRequestError": "bad_request",
    "InvalidRequestError": "bad_request",
    "UnprocessableEntityError": "bad_request",
    "NotFoundError": "bad_request",
    "ConflictError": "conflict",
    "CircuitOpenError": "circuit_open",
    "LLMDeadlineExceededError": "deadline",
    "LLMRetryBudgetExceededError": "retry_budget",
    "RateLimitError": "rate_limit",
    "APITimeoutError": "timeout",
    "TimeoutError": "timeout",
    "TimeoutException": "timeout",
    "ConnectTimeout": "timeout",
    "ReadTimeout": "timeout",
    "WriteTimeout": "timeout",
    "PoolTimeout": "timeout",
    "APIConnectionError": "connection",
    "ConnectionError": "connection",
    "ConnectError": "connection",
    "ReadError": "connection",
    "WriteError": "connection",
    "RemoteProtocolError": "connection",
    "InternalServerError": "server_error",
    "ServiceUnavailableError": "server_error",
}

_NEVER_REPLAY_CATEGORIES = frozenset(
    {
        "auth",
        "permission",
        "bad_request",
        "config",
        "output_budget",
        "deadline",
        "retry_budget",
    }
)
_GATEWAY_RETRY_CATEGORIES = frozenset(
    {"timeout", "rate_limit", "conflict", "connection", "server_error"}
)
_FAILOVER_CATEGORIES = frozenset(
    {"timeout", "rate_limit", "connection", "server_error", "circuit_open"}
)
_BREAKER_FAILURE_CATEGORIES = frozenset({"timeout", "connection", "server_error"})
# Backward-compatible export: failover-eligible availability categories. Request-specific 409
# conflicts stay retryable on the same provider but do not switch provider or affect its health.
AVAILABILITY_ERROR_CATEGORIES = _FAILOVER_CATEGORIES


def _exception_chain(exc: Exception) -> tuple[Exception, ...]:
    """Return *exc*, causes and contexts once each, even for malformed cyclic chains."""
    pending: list[Exception] = [exc]
    result: list[Exception] = []
    seen: set[int] = set()
    while pending:
        current = pending.pop(0)
        identity = id(current)
        if identity in seen:
            continue
        seen.add(identity)
        result.append(current)
        # Match Python chaining semantics: an explicit cause supersedes the implicit context.
        # Following both would let a suppressed, unrelated context override the stated cause.
        link = current.__cause__
        if link is None and not current.__suppress_context__:
            link = current.__context__
        if isinstance(link, Exception) and id(link) not in seen:
            pending.append(link)
    return tuple(result)


def _provider_status_code(exc: Exception) -> int | None:
    """Best-effort explicit HTTP status extraction; never guesses from exception prose."""
    try:
        status = getattr(exc, "status_code", None)
        if status is None:
            status = getattr(getattr(exc, "response", None), "status_code", None)
        return int(status) if status is not None else None
    except Exception:
        return None


def _retry_hint_value(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "yes", "on"}:
        return True
    if normalized in {"false", "0", "no", "off"}:
        return False
    return None


def _retry_hint(exc: Exception) -> bool | None:
    """Read SDK attributes or case-insensitive ``x-should-retry`` response headers."""
    hints: list[bool] = []
    try:
        for attr in ("x_should_retry", "should_retry"):
            hint = _retry_hint_value(getattr(exc, attr, None))
            if hint is not None:
                hints.append(hint)
        response = getattr(exc, "response", None)
        for headers in (getattr(exc, "headers", None), getattr(response, "headers", None)):
            if headers is None:
                continue
            getter = getattr(headers, "get", None)
            if callable(getter):
                for key in ("x-should-retry", "X-Should-Retry"):
                    hint = _retry_hint_value(getter(key))
                    if hint is not None:
                        hints.append(hint)
            items = getattr(headers, "items", None)
            if callable(items):
                for key, value in items():
                    if str(key).lower() == "x-should-retry":
                        hint = _retry_hint_value(value)
                        if hint is not None:
                            hints.append(hint)
    except Exception:
        pass
    return False if False in hints else (True if True in hints else None)


def _retry_after_seconds(
    exc: Exception, *, wall_clock: Callable[[], float] = time.time
) -> float | None:
    """Return a valid ``Retry-After`` delay from an error chain (seconds or HTTP-date).

    Invalid/negative values are ignored. When several wrapper layers expose the header, the
    longest valid delay wins so an adapter cannot accidentally shorten the provider's instruction.
    """
    delays: list[float] = []
    for item in _exception_chain(exc):
        try:
            response = getattr(item, "response", None)
            for headers in (getattr(item, "headers", None), getattr(response, "headers", None)):
                if headers is None:
                    continue
                values: list[Any] = []
                getter = getattr(headers, "get", None)
                if callable(getter):
                    values.extend((getter("retry-after"), getter("Retry-After")))
                items = getattr(headers, "items", None)
                if callable(items):
                    values.extend(
                        value for key, value in items() if str(key).lower() == "retry-after"
                    )
                for value in values:
                    if value is None:
                        continue
                    try:
                        delay = float(str(value).strip())
                    except (TypeError, ValueError):
                        try:
                            parsed = parsedate_to_datetime(str(value).strip())
                            if parsed.tzinfo is None:
                                parsed = parsed.replace(tzinfo=UTC)
                            delay = parsed.timestamp() - wall_clock()
                        except (TypeError, ValueError, OverflowError):
                            continue
                    if delay >= 0:
                        delays.append(delay)
        except Exception:
            continue
    return max(delays) if delays else None


def _status_category(status: int | None) -> str | None:
    if status == 401:
        return "auth"
    if status == 403:
        return "permission"
    if status == 408:
        return "timeout"
    if status == 409:
        return "conflict"
    if status == 429:
        return "rate_limit"
    if status is not None and 400 <= status < 500:
        return "bad_request"
    if status is not None and 500 <= status < 600:
        return "server_error"
    return None


def _explicit_category(exc: Exception) -> str | None:
    try:
        raw = str(getattr(exc, "category", "") or "").strip().lower().replace("-", "_")
    except Exception:
        return None
    return _CATEGORY_ALIASES.get(raw)


def _exact_type_category(exc: Exception) -> str | None:
    for cls in type(exc).__mro__:
        category = _EXACT_EXCEPTION_CATEGORIES.get(cls.__name__)
        if category is not None:
            return category
    return None


def _message_category(exc: Exception) -> str | None:
    """Conservative fallback for untyped legacy errors; no bare number/keyword matching."""
    try:
        message = " ".join(str(exc).lower().split())
    except Exception:
        return None
    status_match = re.search(
        r"\b(?:http(?:\s+status)?|status(?:[_ ]+code)?|error\s+code)\s*[:=]?\s*(\d{3})\b",
        message,
    )
    if status_match is not None:
        category = _status_category(int(status_match.group(1)))
        if category is not None:
            return category
    phrases = (
        ("auth", ("unauthorized", "invalid api key", "incorrect api key")),
        ("permission", ("permission denied", "access forbidden")),
        ("bad_request", ("bad request", "invalid request")),
        ("rate_limit", ("rate limit exceeded", "rate limited", "too many requests")),
        ("conflict", ("request conflict", "resource conflict")),
        (
            "timeout",
            (
                "request timed out",
                "read timed out",
                "connection timed out",
                "operation timed out",
                "provider timed out",
                "deadline exceeded",
                "gateway timeout",
            ),
        ),
        (
            "connection",
            (
                "connection refused",
                "connection reset",
                "connection aborted",
                "failed to connect",
                "could not connect",
                "network is unreachable",
                "remote disconnected",
            ),
        ),
        (
            "server_error",
            ("internal server error", "service unavailable", "bad gateway", "upstream unavailable"),
        ),
    )
    for category, candidates in phrases:
        if any(phrase in message for phrase in candidates):
            return category
    return None


def provider_error_info(exc: Exception) -> ProviderErrorInfo:
    """Classify one error chain in precedence order, preserving explicit provider retry hints."""
    chain = _exception_chain(exc)
    if any(isinstance(item, OutputTruncatedError) for item in chain):
        return ProviderErrorInfo(category="output_budget", retry_hint=False)

    hints = tuple(hint for item in chain if (hint := _retry_hint(item)) is not None)
    # A provider's explicit "do not replay" instruction wins over a conflicting true hint in any
    # wrapper/cause layer. This mirrors the OpenAI-compatible retry contract conservatively.
    retry_hint = False if False in hints else (True if True in hints else None)
    first_status = next(
        (status for item in chain if (status := _provider_status_code(item)) is not None), None
    )

    for item in chain:
        category = _explicit_category(item)
        if category is not None:
            item_status = _provider_status_code(item)
            return ProviderErrorInfo(
                category,
                retry_hint,
                item_status if item_status is not None else first_status,
            )
    for item in chain:
        status = _provider_status_code(item)
        category = _status_category(status)
        if category is not None:
            return ProviderErrorInfo(category, retry_hint, status)
    for item in chain:
        category = _exact_type_category(item)
        if category is not None:
            return ProviderErrorInfo(category, retry_hint, first_status)
    for item in chain:
        category = _message_category(item)
        if category is not None:
            return ProviderErrorInfo(category, retry_hint, first_status)
    return ProviderErrorInfo("provider_error", retry_hint, first_status)


def _classify_provider_error(exc: Exception) -> str:
    """Backward-compatible category-only facade over :func:`provider_error_info`."""
    return provider_error_info(exc).category


def should_gateway_retry(exc: Exception) -> bool:
    """Request-level replay policy for a bare legacy provider."""
    info = provider_error_info(exc)
    if info.category in _NEVER_REPLAY_CATEGORIES or info.retry_hint is False:
        return False
    if info.retry_hint is True:
        return True
    return info.category in _GATEWAY_RETRY_CATEGORIES


def should_failover(exc: Exception) -> bool:
    """Whether a failover composite may hand this request to its secondary exactly once.

    A default 409 is request/state-specific, so replay may succeed on the same provider but a
    provider switch is not assumed useful. An explicit positive retry hint may opt it in.
    """
    info = provider_error_info(exc)
    if info.category in _NEVER_REPLAY_CATEGORIES or info.retry_hint is False:
        return False
    if info.retry_hint is True:
        return True
    return info.category in _FAILOVER_CATEGORIES


def should_count_breaker_failure(exc: Exception) -> bool:
    """Provider-health policy; tenant-scoped 429/409 do not open the global breaker by default."""
    info = provider_error_info(exc)
    if info.retry_hint is False:
        return False
    return info.category in _BREAKER_FAILURE_CATEGORIES


def is_availability_error(exc: Exception) -> bool:
    """Compatibility alias for the failover-eligible availability predicate."""
    return should_failover(exc)
