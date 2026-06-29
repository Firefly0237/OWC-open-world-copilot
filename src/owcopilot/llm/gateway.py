"""THE single chokepoint for every model call in the system.

Cache, router and telemetry all hang off this one method, and call sites never touch a provider
directly — so caching, routing and cost controls are added here once and nothing else changes.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

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


class OpenAICompatProvider:
    """Real provider for OpenAI-compatible APIs (e.g. DeepSeek). Requires `openai` and a key.

    Register one per tier with the real model id, e.g.:
        providers = {
            "cheap":    OpenAICompatProvider(model="deepseek-v4-flash"),
            "frontier": OpenAICompatProvider(model="deepseek-v4-pro"),
        }
    (The older `deepseek-chat` / `deepseek-reasoner` aliases route to V4-Flash and are being
    retired — prefer the explicit `deepseek-v4-flash` / `deepseek-v4-pro` ids.)
    JSON mode is on by default so structured generation parses cleanly. `complete` returns a
    5-tuple: text, prompt/completion tokens, `prompt_cache_hit_tokens` (so the gateway can price
    server-cached prefix tokens at the cheap cache-hit rate), and the response-body `model` id (so
    OTEL `gen_ai.response.model` reflects the model that actually answered).

    Native tool-calling: this provider declares ``supports_tools = True`` and implements
    ``complete_with_tools`` against the OpenAI tools / ``tool_calls`` contract, so an agent opting
    into native function-calling drives a structured loop. The plain text ``complete`` path is
    untouched.
    """

    # Capability flag probed by ``provider_supports_tools``: this provider speaks the OpenAI
    # function-calling contract. Offline fakes leave this unset (probe → False) and the agent
    # falls back to text ReAct.
    supports_tools = True

    def __init__(
        self,
        model: str,
        *,
        base_url_env: str = "OPENAI_BASE_URL",
        api_key_env: str = "OPENAI_API_KEY",
        json_mode: bool = True,
        timeout: float | None = None,
        max_output_tokens: int | None = None,
    ):
        self.model = model
        self.base_url = os.getenv(base_url_env)
        self.api_key = os.getenv(api_key_env)
        self.json_mode = json_mode
        self.timeout = (
            timeout
            if timeout is not None
            else float(os.getenv("OWCOPILOT_PROVIDER_TIMEOUT_SEC", "30"))
        )
        # Cap runaway completions (cost + latency guard). Round-2 real testing saw a verbose
        # draft burn 2238 output tokens / ~24s; the default leaves headroom above that while
        # stopping multi-thousand-token runaways. A truncated JSON fails parsing and falls into
        # the existing tolerant/retry paths, which is the intended trade.
        self.max_output_tokens = (
            max_output_tokens
            if max_output_tokens is not None
            else int(os.getenv("OWCOPILOT_MAX_OUTPUT_TOKENS", "3000"))
        )

    def _wants_json(self, system: str, user: str) -> bool:
        """OpenAI/DeepSeek reject a json_object response_format unless the prompt itself
        mentions 'json'. Our generate/repair prompts do ("Return ONE JSON object…"); the
        planner prompt doesn't — and its output is discarded — so gate JSON mode on the prompt
        to avoid a 400 on the cheap tier when it serves the plan task."""
        return self.json_mode and ("json" in f"{system}\n{user}".lower())

    def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int, int, str]:
        # Fail fast with an actionable message instead of a cryptic SDK error. A common footgun: an
        # existing shell OPENAI_API_KEY overrides .env (load_dotenv uses setdefault), so a shell key
        # for a different provider silently shadows the .env one — name that explicitly.
        if not self.api_key:
            raise RuntimeError(
                "real LLM mode needs an API key, but OPENAI_API_KEY is empty. Set it (and "
                "OPENAI_BASE_URL for a non-OpenAI provider) in your environment or .env. Note: an "
                "existing shell OPENAI_API_KEY takes precedence over .env."
            )
        from openai import OpenAI  # lazy import: offline runs never need this

        client: Any = OpenAI(base_url=self.base_url, api_key=self.api_key, timeout=self.timeout)
        kwargs: dict[str, Any] = (
            {"response_format": {"type": "json_object"}} if self._wants_json(system, user) else {}
        )
        if self.max_output_tokens > 0:
            kwargs["max_tokens"] = self.max_output_tokens
        try:
            resp = client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                **kwargs,
            )
        except Exception as exc:
            if "max_tokens" not in str(exc) and "max_completion_tokens" not in str(exc):
                raise
            fallback_kwargs = dict(kwargs)
            fallback_kwargs.pop("max_tokens", None)
            if self.max_output_tokens > 0:
                fallback_kwargs["max_completion_tokens"] = self.max_output_tokens
            resp = client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                **fallback_kwargs,
            )
        text = resp.choices[0].message.content or ""
        usage = resp.usage
        prompt_tokens = usage.prompt_tokens if usage else 0
        completion_tokens = usage.completion_tokens if usage else 0
        # DeepSeek reports prompt_tokens = prompt_cache_hit_tokens + prompt_cache_miss_tokens.
        # getattr keeps this safe against providers that omit the field.
        cache_hit_tokens = int(getattr(usage, "prompt_cache_hit_tokens", 0) or 0) if usage else 0
        # The model the API *actually answered with* (response body), distinct from the configured
        # `self.model` (what we requested) and the tier label. OTEL gen_ai.response.model reads this
        # so a failover to a secondary surfaces the secondary's model id with no wrapper guessing
        # who answered. Empty string when the body omits it (the gateway treats "" as "unknown").
        response_model = str(getattr(resp, "model", "") or "")
        return text, prompt_tokens, completion_tokens, cache_hit_tokens, response_model

    def complete_with_tools(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str,  # noqa: ARG002 — tier label, kept for protocol symmetry with complete()
    ) -> ToolCallResponse:
        """One native function-calling turn against the OpenAI ``tools`` / ``tool_calls`` contract.

        *messages* is the running OpenAI-format conversation (system + user + prior
        assistant/tool turns); *tools* is the OpenAI tool schema list. Returns a
        :class:`ToolCallResponse`: either a set of requested ``tool_calls`` (the model wants to act)
        or a final ``text`` answer. JSON response_format is deliberately NOT set here — it is
        incompatible with tool-calling on these APIs (the model emits tool_calls, not a JSON body).
        """
        if not self.api_key:
            raise RuntimeError(
                "real LLM mode needs an API key, but OPENAI_API_KEY is empty. Set it (and "
                "OPENAI_BASE_URL for a non-OpenAI provider) in your environment or .env. Note: an "
                "existing shell OPENAI_API_KEY takes precedence over .env."
            )
        from openai import OpenAI  # lazy import: offline runs never need this

        client: Any = OpenAI(base_url=self.base_url, api_key=self.api_key, timeout=self.timeout)
        kwargs: dict[str, Any] = {"tools": tools, "tool_choice": "auto"}
        if self.max_output_tokens > 0:
            kwargs["max_tokens"] = self.max_output_tokens
        try:
            resp = client.chat.completions.create(model=self.model, messages=messages, **kwargs)
        except Exception as exc:
            if "max_tokens" not in str(exc) and "max_completion_tokens" not in str(exc):
                raise
            fallback_kwargs = dict(kwargs)
            fallback_kwargs.pop("max_tokens", None)
            if self.max_output_tokens > 0:
                fallback_kwargs["max_completion_tokens"] = self.max_output_tokens
            resp = client.chat.completions.create(
                model=self.model, messages=messages, **fallback_kwargs
            )
        return _parse_tool_response(resp)


def _parse_tool_response(resp: Any) -> ToolCallResponse:
    """Decode an OpenAI chat-completion response into a :class:`ToolCallResponse`.

    Split out from the provider so it can be unit-tested against a hand-built response object
    without a live API. A malformed ``arguments`` JSON string degrades to ``{}`` (the registry
    then reports the missing-required-arg error as an observation) rather than crashing the loop.
    """
    import json as _json  # noqa: PLC0415

    message = resp.choices[0].message
    text = getattr(message, "content", None) or ""
    raw_calls = getattr(message, "tool_calls", None) or []
    parsed_calls: list[ToolCall] = []
    for tc in raw_calls:
        fn = getattr(tc, "function", None)
        name = getattr(fn, "name", "") if fn is not None else ""
        raw_args = getattr(fn, "arguments", "") if fn is not None else ""
        try:
            args = _json.loads(raw_args) if raw_args else {}
        except (ValueError, TypeError):
            args = {}
        if not isinstance(args, dict):
            args = {}
        parsed_calls.append(
            ToolCall(call_id=str(getattr(tc, "id", "") or ""), name=str(name or ""), arguments=args)
        )
    usage = resp.usage
    prompt_tokens = usage.prompt_tokens if usage else 0
    completion_tokens = usage.completion_tokens if usage else 0
    cache_hit_tokens = int(getattr(usage, "prompt_cache_hit_tokens", 0) or 0) if usage else 0
    response_model = str(getattr(resp, "model", "") or "")
    return ToolCallResponse(
        text=text,
        tool_calls=parsed_calls,
        input_tokens=prompt_tokens,
        output_tokens=completion_tokens,
        cached_input_tokens=cache_hit_tokens,
        response_model=response_model,
    )


class LLMGatewayError(RuntimeError):
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
        for attempt in range(1, attempts + 1):
            try:
                return provider.complete(system=system, user=user, model=tier)
            except Exception as e:
                last_exc = e
                if attempt >= attempts:
                    break
                if self.retry_backoff_seconds:
                    time.sleep(self.retry_backoff_seconds * attempt)
        assert last_exc is not None
        raise LLMGatewayError(
            task=task,
            tier=tier,
            category=_classify_provider_error(last_exc),
            attempts=attempts,
            cause=last_exc,
        ) from last_exc

    def complete_with_tools(
        self,
        *,
        task: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        tier: str | None = None,
    ) -> ToolCallResponse:
        """Native function-calling sibling of :meth:`complete` — routes, calls the provider's
        ``complete_with_tools``, and records telemetry (same cost/latency accounting as the text
        path). Used only by the opt-in native-tools ReAct loop.

        Deliberately does NOT consult the client cache: a tool turn carries the full running message
        history, so a per-prompt cache key would be unsound (and tool side-effects must re-run).
        Telemetry (tokens, model, response_model) is still recorded for every turn, so cost stays
        honest. Raises a guided :class:`LLMGatewayError` if the routed provider lacks native tool
        support — the caller (ReActAgent) probes ``provider_supports_tools`` first and falls back to
        text, so this only fires on a genuine misconfiguration.
        """
        tier, provider = self._resolve_provider(task=task, tier=tier)
        if not provider_supports_tools(provider):
            raise LLMGatewayError(
                task=task,
                tier=tier,
                category="config",
                attempts=0,
                cause=RuntimeError(
                    f"tier {tier!r} provider does not support native tool-calling "
                    f"(no complete_with_tools). Use the text ReAct path, or register a "
                    f"tool-capable provider for {tier!r}."
                ),
            )
        model = getattr(provider, "model", None) or tier
        t0 = time.perf_counter()
        # complete_with_tools is an OPTIONAL protocol member (not on the LLMProvider Protocol), so
        # the static type can't see it. The provider_supports_tools guard above proves it exists
        # and is callable; cast to Any to call it without weakening the Protocol for text providers.
        tool_provider: Any = provider
        try:
            resp = tool_provider.complete_with_tools(messages=messages, tools=tools, model=tier)
        except Exception as exc:
            raise LLMGatewayError(
                task=task,
                tier=tier,
                category=_classify_provider_error(exc),
                attempts=1,
                cause=exc,
            ) from exc
        self.telemetry.record(
            CallRecord(
                task=task,
                tier=tier,
                input_tokens=resp.input_tokens,
                output_tokens=resp.output_tokens,
                cached_input_tokens=resp.cached_input_tokens,
                latency_ms=(time.perf_counter() - t0) * 1000,
                model=model,
                response_model=resp.response_model,
            )
        )
        return resp


def _classify_provider_error(exc: Exception) -> str:
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
