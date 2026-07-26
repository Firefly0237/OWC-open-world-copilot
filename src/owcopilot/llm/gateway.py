# 全系统每次模型调用的唯一咽喉点。
# 关键：缓存/路由/重试/成本遥测/OTEL/原生 tools 都收束在这一层——换供应商、限成本、加观测只改这里。
"""THE single chokepoint for every model call in the system.

Cache, router and telemetry all hang off this one method, and call sites never touch a provider
directly — so caching, routing and cost controls are added here once and nothing else changes.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
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
    def complete(self, *, system: str, user: str, model: str) -> tuple:
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

    def __init__(self, *, task: str, tier: str, category: str, attempts: int, cause: Exception):
        self.task = task
        self.tier = tier
        self.category = category
        self.attempts = attempts
        self.cause = cause
        super().__init__(
            f"LLM provider call failed after {attempts} attempt(s): "
            f"task={task!r}, tier={tier!r}, category={category}, cause={cause}"
        )


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
        namespace: str = "",
    ):
        self.providers = providers
        self.router = router or StaticRouter()
        self.cache = cache or NoOpCache()
        self.telemetry = telemetry or TelemetryCollector()
        self.max_retries = max(0, max_retries)
        self.retry_backoff_seconds = max(0.0, retry_backoff_seconds)
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
                attempts=0,
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

        result = self._complete_with_retries(
            provider, task=task, tier=tier, system=system, user=user
        )
        # Providers return (text, in, out[, cached_in[, response_model]]); element 4 is the
        # provider's server-side prefix-cache hit tokens (priced at the cheap cache-hit rate), and
        # element 5 is the model id from the API response body (the model that actually answered).
        text, in_tok, out_tok = result[0], result[1], result[2]
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
            )
        )
        if text:  # never memoize an empty/failed completion
            self.cache.set(key, text)
        return text

    def _complete_with_retries(
        self, provider: LLMProvider, *, task: str, tier: str, system: str, user: str
    ) -> tuple:
        attempts = self.max_retries + 1
        last_exc: Exception | None = None
        made = 0
        for attempt in range(1, attempts + 1):
            made = attempt
            try:
                return provider.complete(system=system, user=user, model=tier)
            except Exception as e:
                last_exc = e
                # A capped-output reply will almost certainly be capped again on retry — fail
                # fast with the guided message instead of paying for another truncated call.
                if isinstance(e, OutputTruncatedError) or attempt >= attempts:
                    break
                if self.retry_backoff_seconds:
                    time.sleep(self.retry_backoff_seconds * attempt)
        assert last_exc is not None
        raise LLMGatewayError(
            task=task,
            tier=tier,
            category=_classify_provider_error(last_exc),
            attempts=made,
            cause=last_exc,
        ) from last_exc


def _classify_provider_error(exc: Exception) -> str:
    if isinstance(exc, OutputTruncatedError):
        return "output_budget"
    name = exc.__class__.__name__.lower()
    msg = str(exc).lower()
    if "timeout" in name or "timeout" in msg or "timed out" in msg:
        return "timeout"
    if "rate" in name and "limit" in name or "rate limit" in msg or "429" in msg:
        return "rate_limit"
    if "auth" in name or "unauthorized" in msg or "401" in msg:
        return "auth"
    if "connection" in name or "connect" in msg:
        return "connection"
    return "provider_error"
