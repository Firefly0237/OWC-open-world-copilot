"""THE single chokepoint for every model call in the system.

Cache, router and telemetry all hang off this one method, and call sites never touch a provider
directly — so caching, routing and cost controls are added here once and nothing else changes.
"""

from __future__ import annotations

import os
import time
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


def require_offline_llm_allowed() -> None:
    """Fail closed before a runtime path would hand back fake LLM output to a real caller."""
    if not offline_llm_allowed():
        raise RuntimeError(OFFLINE_LLM_FORBIDDEN_MESSAGE)


class LLMProvider(Protocol):
    def complete(self, *, system: str, user: str, model: str) -> tuple:
        """Return (text, input_tokens, output_tokens[, cached_input_tokens]).

        The 4th element is optional: a provider that knows its server-side prefix-cache
        usage (e.g. DeepSeek's `prompt_cache_hit_tokens`) returns it so the gateway can
        price those tokens at the cheaper cache-hit rate. Providers that don't (the offline
        fakes) return the 3-tuple and the gateway treats cached tokens as 0. `model` is the
        tier label.
        """
        ...


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
    4-tuple including `prompt_cache_hit_tokens` so the gateway can price server-cached prefix
    tokens at the cheap cache-hit rate.
    """

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

    def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int, int]:
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
            if "max_tokens" not in str(exc) or "max_completion_tokens" not in str(exc):
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
        return text, prompt_tokens, completion_tokens, cache_hit_tokens


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

    def complete(self, *, task: str, system: str, user: str, tier: str | None = None) -> str:
        tier = self.router.choose(task=task, hint=tier)
        provider = self.providers[tier]
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
                )
            )
            return cached

        result = self._complete_with_retries(
            provider, task=task, tier=tier, system=system, user=user
        )
        # Providers return (text, in, out) or (text, in, out, cached_in); the 4th is the
        # provider's server-side prefix-cache hit tokens (priced at the cheap cache-hit rate).
        text, in_tok, out_tok = result[0], result[1], result[2]
        cached_in = result[3] if len(result) > 3 else 0
        self.telemetry.record(
            CallRecord(
                task=task,
                tier=tier,
                input_tokens=in_tok,
                output_tokens=out_tok,
                cached_input_tokens=cached_in,
                latency_ms=(time.perf_counter() - t0) * 1000,
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
